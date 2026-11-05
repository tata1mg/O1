"""Kiwi TCMS `CaseStore` — `kiwi_writer.upsert_cases` + `kiwi_runner.create_run/record_execution`,
merged behind one protocol.

Ported from test_agent importer/kiwi_writer.py and importer/kiwi_runner.py @ ca7d4ce. `tcms_api` is
imported lazily inside `_rpc()` so importing this module (or the whole workflow) needs no dependency —
that matters because `workflows.testing` is scanned at CLI startup.

Deliberate changes from test_agent, all recorded in the workflow README:

  1. **No password default.** kiwi_runner.py:28-30 and be_requirements.py:135-137 shipped the admin
     password as a literal. Credentials come from flags (env `TCMS_*`) or ~/.tcms.conf, and an incomplete
     triple falls back to ~/.tcms.conf rather than guessing.
  2. **One product-matching rule.** test_agent had three (kiwi_writer: exact name; kiwi_runner/be_spec:
     `endswith("/ <feature>") or endswith(feature)`; be_requirements: substring `feature in name`), so a
     product that resolved for the writer could resolve to a DIFFERENT product for the reader. The single
     rule is exact match on `<product_prefix><feature>` — the name the writer creates — with the
     `endswith` form kept only as a fallback for products created before the prefix was configurable.
  3. **TLS verification is a flag AND per-connection** (`testing.insecure_tls`, default True to match
     today). test_agent monkeypatched `ssl._create_default_https_context` at import time in four
     modules, which is process-global and permanent: once any kiwi code ran, every later HTTPS call in
     the process stopped verifying. Here an `ssl.SSLContext` is built for this transport only, the
     process default is restored in a `finally`, and the global default is left as it was found.
  4. **Templates are flags**: product prefix, plan name, run summary.
  5. **Cases are uploaded PROPOSED and a human edit is never overwritten** (Phase 1). test_agent set every
     case CONFIRMED, then re-CONFIRMED them all after `TestPlan.add_case`, so a re-upload reset whatever a
     reviewer had decided. Kiwi's `add_case` only links the case (tcms/testplans/models.py) and leaves its
     status alone, so the extra pass is gone too.
  6. **Execution timestamps carry an offset**, and the store can attach files — both ported from
     test_agent after the original port (0188771).

Everything else — the ensure-by-name walk, the `extid:`/`feature:` idempotency key, the start_date/stop_date
duration trick because `actual_duration` is read-only, the 500/900 property truncation — is unchanged.
"""

from __future__ import annotations

import configparser
import html
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import STATUS, Case, Run, check_scope


def _apply_context(client, context) -> bool:
    """Attach an SSLContext to whatever transport the client built. -> did it land?

    `xmlrpc.client.ServerProxy` keeps its transport in the name-mangled `_ServerProxy__transport`, and
    `SafeTransport` consults `self.context` per connection. tcms-api wraps a ServerProxy but the
    attribute path has moved between versions, so probe rather than assume — and never raise: failing to
    attach is a fallback, not an error.
    """
    seen = False
    for holder in (client, getattr(client, "exec", None), getattr(client, "_server", None)):
        if holder is None:
            continue
        for attr in ("_ServerProxy__transport", "transport", "_transport"):
            transport = getattr(holder, attr, None)
            if transport is not None and hasattr(transport, "context"):
                try:
                    transport.context = context
                    seen = True
                except (AttributeError, TypeError):
                    pass
    return seen


#: sheet Priority -> Kiwi Priority value (kiwi_writer.py:26).
PRIORITY_MAP = {"high": "P1", "medium": "P3", "low": "P5", "critical": "P1"}
#: A TestExecution property value is `varchar(255)` in Kiwi 16: a longer one is REJECTED ("value too long
#: for type character varying(255)"), not truncated, and `add_property` swallowed that, so every failure
#: reason over 255 characters reached Kiwi as nothing at all (2026-11-04, probed on plutokiwi-agent 16.2).
#: The full text goes in the execution's comment; the property is the one-line summary.
PROP_VALUE_MAX = 255
ACTUAL_VALUE_MAX = 255
SUGGESTION_VALUE_MAX = 2000
#: The only status the agent writes a case with. Approving (CONFIRMED) or rejecting (DISABLED) is a human's.
UPLOAD_STATUS = "PROPOSED"
#: scope -> the Kiwi API object. Every one exposes `list_attachments(id)` and `add_attachment(id, ...)`.
_ATTACH_API = {"execution": "TestExecution", "case": "TestCase", "run": "TestRun", "plan": "TestPlan"}


#: Where tcms-api looks for its config when no url is passed, in its own order (tcms_api/__init__.py).
TCMS_CONF_PATHS = ("~/.tcms.conf", "/etc/tcms.conf")


def _conf_username() -> str:
    """The username tcms-api will log in with from its config file, or "" when it names none.

    Mirrors tcms-api's lookup: the first file that exists wins, even if it has no username.
    """
    for raw in TCMS_CONF_PATHS:
        path = Path(raw).expanduser()
        if path.exists():
            conf = configparser.ConfigParser()
            conf.read(path)
            return conf.get("tcms", "username", fallback="")
    return ""


#: Tag prefixes the store owns and keeps in step with the suite. `extid:`/`feature:` never change for a case,
#: and a tag with none of these prefixes is a person's, so it is left alone.
MANAGED_TAG_PREFIXES = ("scenario:", "surface:", "exec:")


def _tag_diff(rpc, case_id, wanted: list[str]) -> tuple[list[str], list[str]]:
    """(stale, missing): managed tags the case carries but the suite no longer gives it, and tags to add.

    WHY: the store used to only ADD tags, so a case re-tagged `manual` -> `http` kept both (2026-11-02,
    NR-DIGICHEM: 19 cases carried `exec:manual` and `exec:http`). `create_run` selects on `exec:http`
    alone, so a case re-tagged the other way would still have been executed.
    """
    have = [t["name"] for t in rpc.Tag.filter({"case": case_id})]
    stale = [t for t in have if t.startswith(MANAGED_TAG_PREFIXES) and t not in wanted]
    return stale, [t for t in wanted if t not in have]


def _cap(text: str, limit: int) -> str:
    """One line, at most `limit` characters — what fits Kiwi's varchar(255) property column."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _unescape(value) -> str:
    """Kiwi hands back what it stored HTML-escaped (`'` -> `&#x27;`, `"` -> `&quot;`). Read it as written."""
    return html.unescape(str(value if value is not None else ""))


def _changed_fields(existing: dict, fields: dict) -> dict:
    """{field: new value} for each field whose stored value differs. Ids compare as strings, because
    XML-RPC hands back ints where the writer may hold either; text compares unescaped, because otherwise
    every case with a quote in it read as changed and was rewritten on every upload (2026-11-04: 71 of 98)."""
    return {k: v for k, v in fields.items() if _unescape(existing.get(k, "")) != str(v)}


def _basename(url: str) -> str:
    return url.rsplit("/", 1)[-1]


class KiwiCaseStore:
    name = "kiwi"

    def __init__(self, *, url: str = "", user: str = "", password: str = "",
                 web: str = "https://localhost:8443", insecure: bool = True,
                 product_prefix: str = "Diagno / ",
                 plan_template: str = "{feature} — imported test cases",
                 run_template: str = "{feature} · {env} · {build}", rpc=None) -> None:
        self.url, self.user, self.password, self.web = url, user, password, web
        self.insecure = insecure
        self.product_prefix = product_prefix
        self.plan_template = plan_template
        self.run_template = run_template
        self._rpc_obj = rpc  # injectable for tests; None -> connect on first use
        #: whether `_apply_context` managed to attach our SSLContext to the transport. False with
        #: `insecure=True` means the connection relied on the narrowly-scoped default swap instead.
        self._context_attached: bool | None = None

    # ---- connection -------------------------------------------------------------------------------
    def _tls_context(self):
        """An unverified `SSLContext` for THIS connection, or None when verification stays on.

        WHY not the monkeypatch: `ssl._create_default_https_context = ssl._create_unverified_context` is
        what test_agent did (in four modules, at import time), and it is process-global and permanent.
        Once any kiwi-backed stage ran, every later HTTPS call in the process — Atlassian, Figma, an
        engine adapter fetching anything — silently stopped verifying certificates. A context object
        passed to one transport cannot leak that way.
        """
        if not self.insecure:
            return None
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _client(self, tcms_cls, context):
        """Build the tcms-api client, attaching `context` to its transport when it has one.

        tcms-api's own constructor takes no `context`, and which transport class it builds differs by
        version, so the context is attached after the fact. `_apply_context` reports whether it landed;
        when it did not, we fall back to a NARROWLY scoped swap of the process default — restored in a
        `finally` — rather than leaving verification on against a cert we know will be rejected.
        """
        def connect():
            if self.url and self.user and self.password:
                return tcms_cls(self.url, self.user, self.password)
            # ~/.tcms.conf — tcms-api's own default. The agent is whoever that file logs in as, so its
            # own edits are told apart from a reviewer's without also setting `testing.kiwi_user`.
            self.user = _conf_username() or self.user
            return tcms_cls()

        if context is None:
            return connect()
        import ssl

        # Pre-seed the default so the handshake tcms-api performs DURING construction also uses our
        # context, then restore it immediately. The window is this call, not the process's lifetime.
        previous = ssl._create_default_https_context
        ssl._create_default_https_context = lambda *a, **kw: context
        try:
            client = connect()
        finally:
            ssl._create_default_https_context = previous
        self._context_attached = _apply_context(client, context)
        return client

    def _rpc(self):
        if self._rpc_obj is not None:
            return self._rpc_obj
        from tcms_api import TCMS

        self._rpc_obj = self._client(TCMS, self._tls_context()).exec
        return self._rpc_obj

    # ---- name resolution --------------------------------------------------------------------------
    def product_name(self, feature: str) -> str:
        return f"{self.product_prefix}{feature}"

    def _product(self, rpc, feature: str) -> dict:
        """THE single product-matching rule (see module docstring, change 2)."""
        want = self.product_name(feature)
        products = rpc.Product.filter({})
        exact = [p for p in products if p["name"] == want]
        if exact:
            return exact[0]
        legacy = [p for p in products if p["name"].endswith(f"/ {feature}") or p["name"] == feature]
        if legacy:
            return legacy[0]
        raise RuntimeError(f"kiwi: no product named {want!r} (nor a legacy '*/ {feature}') — "
                           f"upsert the cases first, or fix testing.product_prefix")

    def _plan(self, rpc, product: dict, feature: str) -> dict:
        name = self.plan_template.format(feature=feature)
        plans = rpc.TestPlan.filter({"name": name, "product": product["id"]})
        if plans:
            return plans[0]
        any_plan = rpc.TestPlan.filter({"product": product["id"]})
        if any_plan:
            return any_plan[0]  # a plan created under a different template still holds the cases
        raise RuntimeError(f"kiwi: no test plan for product {product['name']!r}")

    # ---- CaseStore --------------------------------------------------------------------------------
    def upsert_cases(self, feature: str, cases: list[dict]) -> dict:
        """Create/update `cases` under Product '<prefix><feature>' and its plan. Idempotent per extid.

        New cases are created PROPOSED and added to the plan. An existing case is updated only while no
        human has touched it; a touched one is left exactly as the reviewer left it (status, fields, tags
        and plan membership) and the agent's would-be change is posted as a comment on it instead.
        """
        from ..schema import case_tags, render_text

        if not cases:
            return {"created": 0, "updated": 0, "unchanged": 0, "protected": 0, "protected_ids": [],
                    "total": 0, "backend": self.name, "feature": feature, "error": "no cases supplied"}
        rpc = self._rpc()
        product_name = self.product_name(feature)

        def ensure(kind, key, create):
            existing = getattr(rpc, kind).filter(key)
            return existing[0] if existing else getattr(rpc, kind).create(create)

        classes = rpc.Classification.filter({})
        classification = classes[0] if classes else rpc.Classification.create(
            {"name": self.product_prefix.strip(" /") or "Default"})
        product = ensure("Product", {"name": product_name},
                         {"name": product_name, "classification": classification["id"]})
        version = ensure("Version", {"product": product["id"], "value": "unspecified"},
                         {"product": product["id"], "value": "unspecified"})
        plan_type = rpc.PlanType.filter({})[0]
        proposed = rpc.TestCaseStatus.filter({"name": UPLOAD_STATUS})
        if not proposed:
            raise RuntimeError(f"kiwi: no {UPLOAD_STATUS} case status — the review step needs it")
        proposed = proposed[0]
        agent_id = self._agent_user_id(rpc)

        plan_name = self.plan_template.format(feature=feature)
        plans = rpc.TestPlan.filter({"name": plan_name, "product": product["id"]})
        plan = plans[0] if plans else rpc.TestPlan.create({
            "name": plan_name, "text": f"Test cases for {feature}.",
            "product": product["id"], "product_version": version["id"], "type": plan_type["id"],
        })

        created = updated = unchanged = 0
        protected: list[str] = []
        prio_cache, cat_cache = {}, {}
        # Idempotency is scoped BY FEATURE: only match cases already tagged feature:<feature>, so the
        # same TC-id under a different feature never collides.
        feature_case_ids = {x["id"] for x in rpc.TestCase.filter({"tag__name": f"feature:{feature}"})}
        for c in cases:
            pname = PRIORITY_MAP.get((c.get("priority") or "").lower(), "P3")
            if pname not in prio_cache:
                prio_cache[pname] = rpc.Priority.filter({"value": pname})[0]
            mod = c.get("module") or "General"
            if mod not in cat_cache:
                cat_cache[mod] = ensure("Category", {"name": mod, "product": product["id"]},
                                        {"name": mod, "product": product["id"]})
            fields = {
                "summary": f"[{c['id']}] {c.get('title', '')}".strip(),
                "text": render_text(c),
                "category": cat_cache[mod]["id"],
                "priority": prio_cache[pname]["id"],
            }
            found = [x for x in rpc.TestCase.filter({"tag__name": f"extid:{c['id']}"})
                     if x["id"] in feature_case_ids]
            wanted = case_tags(c, feature)
            stale, missing = [], wanted
            if not found:
                # Status is set on create and never again: test_agent re-confirmed every case after
                # `TestPlan.add_case`, which in Kiwi links the case and leaves its status alone — so the
                # extra pass did nothing on a new case and, on a re-upload, overwrote the reviewer's.
                case = rpc.TestCase.create({**fields, "case_status": proposed["id"], "is_automated": False})
                feature_case_ids.add(case["id"])
                rpc.TestPlan.add_case(plan["id"], case["id"])
                created += 1
            else:
                case = found[0]
                changed = _changed_fields(case, fields)
                stale, missing = _tag_diff(rpc, case["id"], wanted)
                if not changed and not stale and not missing:
                    unchanged += 1
                    continue
                touched = self._touched_by_human(rpc, case, agent_id)
                if touched:
                    if stale or missing:
                        changed = {**changed, "tags": " ".join([*(f"-{t}" for t in stale),
                                                                 *(f"+{t}" for t in missing)])}
                    self._suggest(rpc, case["id"], c["id"], touched, changed)
                    protected.append(c["id"])
                    continue
                if changed:
                    rpc.TestCase.update(case["id"], fields)
                updated += 1
            for t in stale:
                rpc.TestCase.remove_tag(case["id"], t)
            for t in missing:
                rpc.TestCase.add_tag(case["id"], t)

        return {
            "created": created, "updated": updated, "unchanged": unchanged, "protected": len(protected),
            "protected_ids": protected, "total": len(cases), "backend": self.name,
            "feature": feature, "product": product_name, "plan": plan_name, "plan_id": plan["id"],
            "plan_url": f"{self.web}/plan/{plan['id']}/", "status": UPLOAD_STATUS,
        }

    def _agent_user_id(self, rpc):
        """The id of the account this store writes as, or None when it cannot be told (no username in the
        flags or in ~/.tcms.conf). None makes every existing case count as human-touched."""
        if not self.user:
            return None
        users = rpc.User.filter({"username": self.user})
        return users[0]["id"] if users else None

    def _touched_by_human(self, rpc, case: dict, agent_id) -> str:
        """Why this case must not be overwritten, or "" when only the agent has ever written it.

        A status other than PROPOSED is a reviewer's decision. Otherwise `TestCase.history` names every
        account that saved the case; anyone but the agent is a human edit. When that cannot be read, or the
        agent's own account is unknown, the answer is "touched": overwriting a reviewer's work is the one
        mistake this rule exists to prevent, and the cost of erring the other way is a comment.
        """
        status = case.get("case_status__name")
        if status and status != UPLOAD_STATUS:
            return f"status is {status}"
        if agent_id is None:
            return "the agent's own Kiwi account is unknown, so its edits cannot be told from a human's"
        try:
            history = rpc.TestCase.history(case["id"], {})
        except Exception as exc:
            return f"its history could not be read ({type(exc).__name__})"
        others = sorted({str(h.get("history_user_id")) for h in history
                         if h.get("history_user_id") not in (None, agent_id)})
        return f"edited by user {', '.join(others)}" if others else ""

    def _suggest(self, rpc, case_id, extid: str, why: str, changed: dict) -> None:
        """Comment the change the agent would have made. Skipped when the same suggestion is already
        there, so re-running an unchanged suite does not stack identical comments."""
        lines = [f"Agent suggestion for {extid}: a regenerated suite would change "
                 f"{', '.join(sorted(changed))}. Not applied because {why}; the human edit wins.", ""]
        for name in sorted(changed):
            lines.append(f"{name}: {str(changed[name])[:SUGGESTION_VALUE_MAX]}")
        text = "\n".join(lines)
        try:
            if any(c.get("comment") == text for c in rpc.TestCase.comments(case_id)):
                return
        except Exception:
            pass
        try:
            rpc.TestCase.add_comment(case_id, text)
        except Exception:
            pass  # a lost suggestion is a missed hint, never a reason to fail the upload

    def list_cases(self, feature: str, *, status: str | None = None,
                   tags: tuple[str, ...] = ()) -> list[Case]:
        rpc = self._rpc()
        product = self._product(rpc, feature)
        plan = self._plan(rpc, product, feature)
        out: list[Case] = []
        for case in rpc.TestCase.filter({"plan": plan["id"]}):
            case_tags_ = tuple(t["name"] for t in rpc.Tag.filter({"case": case["id"]}))
            if status is not None and case["case_status__name"] != status:
                continue
            if any(t not in case_tags_ for t in tags):
                continue
            extid = next((t.split(":", 1)[1] for t in case_tags_ if t.startswith("extid:")), None)
            if not extid:
                continue
            # unescaped: derive_spec parses this text, and `Assert body.x == "ok"` must not arrive as
            # `== &quot;ok&quot;`
            out.append(Case(extid=extid, text=_unescape(case.get("text")),
                            status=case["case_status__name"], tags=case_tags_,
                            summary=_unescape(case.get("summary"))))
        return out

    def create_run(self, feature: str, build: str, env: str, extids: list[str] | None = None,
                   require_tag: str | None = None) -> Run:
        rpc = self._rpc()
        product = self._product(rpc, feature)
        plan = self._plan(rpc, product, feature)

        versions = rpc.Version.filter({"product": product["id"]})
        if not versions:
            raise RuntimeError(f"kiwi: no version for product {product['name']!r}")
        builds = [b for b in rpc.Build.filter({"version": versions[0]["id"]}) if b["name"] == build]
        build_obj = builds[0] if builds else rpc.Build.create({"name": build, "version": versions[0]["id"]})

        summary = self.run_template.format(feature=feature, env=env, build=build)
        existing = [r for r in rpc.TestRun.filter({"plan": plan["id"]}) if r["summary"] == summary]
        if existing:
            run = existing[0]
        else:
            users = rpc.User.filter({"username": self.user}) if self.user else []
            if not users:
                users = rpc.User.filter({})
            if not users:
                raise RuntimeError("kiwi: no user to own the run")
            run = rpc.TestRun.create({
                "summary": summary, "plan": plan["id"], "build": build_obj["id"], "manager": users[0]["id"],
                "notes": f"Created by the testing workflow at "
                         f"{datetime.now(UTC).isoformat(timespec='seconds')}. "
                         f"environment={env} build={build}",
            })

        attached, skipped, untagged = [], [], []
        for case in rpc.TestCase.filter({"plan": plan["id"]}):
            case_tags_ = [t["name"] for t in rpc.Tag.filter({"case": case["id"]})]
            extid = next((t.split(":", 1)[1] for t in case_tags_ if t.startswith("extid:")), None)
            if extids is not None and extid not in extids:
                continue
            if case["case_status__name"] != "CONFIRMED":
                skipped.append(extid)                   # the review gate, enforced here
                continue
            if require_tag and require_tag not in case_tags_:
                exec_tag = next((t for t in case_tags_ if t.startswith("exec:")), None)
                untagged.append(f"{extid} ({exec_tag or 'no exec: tag'})")
                continue                                # the execution-classification gate
            try:
                rpc.TestRun.add_case(run["id"], case["id"])
            except Exception:
                pass                                    # already attached
            attached.append(extid)
        return Run(run_id=str(run["id"]), plan_id=str(plan["id"]), build=build, environment=env,
                   attached=tuple(attached), skipped_unconfirmed=tuple(skipped),
                   skipped_wrong_execution=tuple(untagged), url=f"{self.web}/runs/{run['id']}/")

    def record_execution(self, run_id: str, extid: str, status: str, *, actual: str = "",
                         duration_s: float | None = None, props: dict | None = None,
                         finished_at: datetime | None = None) -> str:
        rpc = self._rpc()
        kiwi_status = STATUS.get(status.upper())
        if not kiwi_status:
            raise ValueError(f"status must be one of {list(STATUS)}, got {status!r}")
        statuses = rpc.TestExecutionStatus.filter({"name": kiwi_status})
        if not statuses:
            raise RuntimeError(f"kiwi: no execution status {kiwi_status}")

        target = None
        for ex in rpc.TestExecution.filter({"run": int(run_id)}):
            if f"extid:{extid}" in [t["name"] for t in rpc.Tag.filter({"case": ex["case"]})]:
                target = ex
                break
        if target is None:
            raise RuntimeError(f"no execution for extid {extid} in run {run_id} — was the case attached?")

        fields = {"status": statuses[0]["id"]}
        if duration_s is not None:
            # actual_duration is READ-ONLY: Kiwi derives it from stop_date - start_date. Writing it
            # directly is accepted and silently discarded (probed 2026-08-25 on Kiwi 15.x), so the
            # measured elapsed time was being thrown away. Set the two timestamps and let Kiwi compute
            # it. Microsecond precision matters: second-truncated strings made a 1.23s call report 2.0s.
            stop = finished_at or datetime.now(UTC)
            if stop.tzinfo is None:
                stop = stop.replace(tzinfo=UTC)
            start = stop - timedelta(seconds=float(duration_s))
            # An EXPLICIT offset, never a naive string. With KIWI_USE_TZ=True Django reads a naive value as
            # the instance's TIME_ZONE, so naive UTC on the Asia/Kolkata deployment was stored 5h30m off.
            # Ported from test_agent kiwi_runner.py (0188771).
            fields["start_date"] = start.isoformat(timespec="microseconds")
            fields["stop_date"] = stop.isoformat(timespec="microseconds")
        rpc.TestExecution.update(target["id"], fields)

        # evidence + metadata. Redact before this point; Kiwi is readable by every account.
        for k, v in (props or {}).items():
            try:
                rpc.TestExecution.add_property(target["id"], str(k), _cap(str(v), PROP_VALUE_MAX))
            except Exception:
                pass
        if actual:
            try:
                rpc.TestExecution.add_property(target["id"], "actual", _cap(actual, ACTUAL_VALUE_MAX))
            except Exception:
                pass
        return str(target["id"])

    def attach(self, scope: str, target_id: str, name: str, content: bytes, *,
               replace: bool = False) -> bool:
        """Ported from test_agent kiwi_runner.add_attachment (0188771): idempotent by name, never raises
        on Kiwi's side of it — a missing attachment is lost evidence, not a failed run."""
        import base64

        check_scope(scope)
        rpc = self._rpc()
        api = getattr(rpc, _ATTACH_API[scope])
        try:
            existing = [a for a in api.list_attachments(int(target_id))
                        if _basename(a.get("url", "")) == name]
        except Exception:
            existing = []  # read-back is an optimisation; attaching is still better than filing nothing
        if existing and not replace:
            return True
        for a in existing:
            try:
                rpc.Attachment.remove_attachment(a["pk"])
            except Exception:
                pass
        try:
            api.add_attachment(int(target_id), name, base64.b64encode(content).decode("ascii"))
            return True
        except Exception:
            return False

    def attachment_url(self, scope: str, target_id: str, name: str) -> str:
        """Read back BY NAME. Falls back to a stem match because Kiwi suffixes a clashing upload
        (`response_qWGCM4Y.json`), and rows filed before attaching became idempotent carry that suffix."""
        check_scope(scope)
        try:
            rows = getattr(self._rpc(), _ATTACH_API[scope]).list_attachments(int(target_id))
        except Exception:
            return ""
        stem, _, suffix = name.rpartition(".")
        fallback = ""
        for a in rows:
            base = _basename(a.get("url", ""))
            if base == name:
                return a.get("url", "")
            if stem and base.startswith(stem) and base.endswith(suffix):
                fallback = a.get("url", "")
        return fallback

    def comment_execution(self, execution_id: str, text: str) -> bool:
        try:
            rpc = self._rpc()
            if any(c.get("comment") == text for c in rpc.TestExecution.get_comments(int(execution_id))):
                return True
            rpc.TestExecution.add_comment(int(execution_id), text)
            return True
        except Exception:
            return False

    def health(self) -> dict:
        try:
            rpc = self._rpc()
            me = rpc.User.filter({"username": self.user}) if self.user else rpc.User.filter({})
            return {"ok": True, "backend": self.name, "url": self.url or "~/.tcms.conf",
                    "users": len(me), "network": True,
                    "tls_verified": not self.insecure,
                    "tls_context_attached": self._context_attached}
        except Exception as exc:  # health must report, never raise
            return {"ok": False, "backend": self.name, "error": f"{type(exc).__name__}: {exc}",
                    "network": True}
