"""Convert Informatica PowerCenter XML mappings into dbt SQL models.

The converter runs in three stages:

1.  **Parse** - read the subset of the PowerCenter schema we care about
    (instances, transformations, ports, connectors) into plain dataclasses.
    Nothing downstream of this stage touches lxml.
2.  **Plan** - treat the mapping as a directed graph, where connectors are the
    edges between instances, and topologically sort it. This is what fixes the
    ordering of the generated CTEs.
3.  **Generate** - emit one CTE per instance in that order, each selecting from
    the CTEs upstream of it, closing with ``select * from final``.

Usage:
    python converter.py mapping.xml --out models/
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

import expressions

# PowerCenter TRANSFORMATION/@TYPE values we know how to translate. Anything
# outside this set becomes a pass-through CTE carrying a TODO comment, so the
# surrounding chain still parses, rather than being silently dropped.
SUPPORTED_TYPES = {
    "Source Qualifier",
    "Expression",
    "Filter",
    "Aggregator",
    "Joiner",
    "Sorter",
}

# Conventional PowerCenter instance-name prefixes, stripped when deriving a CTE
# name (AGG_CUSTOMER_ORDERS -> customer_orders).
_PREFIX_RE = re.compile(
    r"^(SQ|SQTRANS|EXP|FIL|AGG|JNR|SRT|LKP|RTR|UPD|SEQ|NRM|UN|SC|TGT|SRC|T)_", re.I
)

_JOIN_TYPES = {
    "Normal Join": "inner join",
    "Master Outer Join": "left join",
    "Detail Outer Join": "right join",
    "Full Outer Join": "full outer join",
}


class UnsupportedTransformation(Exception):
    """Raised in strict mode when a mapping uses a transformation we cannot translate."""


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class Port:
    """A single field on a transformation."""

    name: str
    datatype: str = ""
    porttype: str = ""  # INPUT, OUTPUT, INPUT/OUTPUT, ...
    expression: str = ""
    group_by: bool = False
    sort_key: bool = False
    sort_descending: bool = False

    @property
    def is_output(self) -> bool:
        return "OUTPUT" in self.porttype.upper()

    @property
    def is_derived(self) -> bool:
        """True when the port computes something rather than passing a value through."""
        expr = self.expression.strip()
        return bool(expr) and expr.upper() != self.name.upper()


@dataclass
class Transformation:
    """A transformation definition: its ports and its table attributes."""

    name: str
    type: str
    ports: list[Port] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def is_supported(self) -> bool:
        return self.type in SUPPORTED_TYPES

    def attribute(self, *names: str) -> str:
        """First matching table attribute, compared case-insensitively."""
        lowered = {k.lower(): v for k, v in self.attributes.items()}
        for name in names:
            if lowered.get(name.lower()):
                return lowered[name.lower()]
        return ""


@dataclass
class Instance:
    """A transformation *used* in a mapping. Connectors wire instances together."""

    name: str
    type: str = ""  # SOURCE, TARGET, TRANSFORMATION
    transformation_name: str = ""
    transformation_type: str = ""

    @property
    def is_source(self) -> bool:
        return self.type.upper() == "SOURCE"

    @property
    def is_target(self) -> bool:
        return self.type.upper() == "TARGET"


@dataclass
class Connector:
    """A wire: (from_instance.from_field) -> (to_instance.to_field)."""

    from_instance: str
    from_field: str
    to_instance: str
    to_field: str


@dataclass
class Mapping:
    name: str
    instances: list[Instance] = field(default_factory=list)
    transformations: dict[str, Transformation] = field(default_factory=dict)
    connectors: list[Connector] = field(default_factory=list)

    def instance(self, name: str) -> Instance | None:
        return next((i for i in self.instances if i.name == name), None)

    def transformation_for(self, inst: Instance) -> Transformation | None:
        return self.transformations.get(inst.transformation_name or inst.name)

    def type_of(self, inst: Instance) -> str:
        if inst.transformation_type:
            return inst.transformation_type
        tf = self.transformation_for(inst)
        return tf.type if tf else ""

    def upstream(self, name: str) -> list[str]:
        """Instances feeding ``name``, in first-connector order, deduplicated."""
        seen: list[str] = []
        for c in self.connectors:
            if c.to_instance == name and c.from_instance not in seen:
                seen.append(c.from_instance)
        return seen

    def downstream(self, name: str) -> list[str]:
        seen: list[str] = []
        for c in self.connectors:
            if c.from_instance == name and c.to_instance not in seen:
                seen.append(c.to_instance)
        return seen

    @property
    def sources(self) -> list[str]:
        return [i.transformation_name or i.name for i in self.instances if i.is_source]

    @property
    def targets(self) -> list[str]:
        return [i.transformation_name or i.name for i in self.instances if i.is_target]

    @property
    def unsupported(self) -> list[Instance]:
        out = []
        for inst in self.instances:
            if inst.is_source or inst.is_target:
                continue
            if self.type_of(inst) not in SUPPORTED_TYPES:
                out.append(inst)
        return out


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _parser() -> etree.XMLParser:
    # PowerCenter exports reference a local DTD that is rarely shipped with the
    # file, so resolution is disabled; recover=True tolerates the malformed
    # exports that repositories sometimes produce.
    return etree.XMLParser(load_dtd=False, resolve_entities=False, recover=True)


def parse_file(path: str | Path) -> list[Mapping]:
    """Parse a PowerCenter export and return every mapping it contains."""
    return parse_tree(etree.parse(str(path), _parser()).getroot())


def parse_string(xml: str | bytes) -> list[Mapping]:
    """Parse a PowerCenter export held in memory. Mirrors :func:`parse_file`."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    return parse_tree(etree.fromstring(xml, _parser()))


def parse_tree(root: etree._Element) -> list[Mapping]:
    # Reusable transformations live at FOLDER level, outside any mapping;
    # non-reusable ones live inside the <MAPPING> that uses them. Collect the
    # reusable pool first so every mapping can resolve instances against it.
    reusable: dict[str, Transformation] = {}
    for el in root.iter("TRANSFORMATION"):
        if _enclosing_mapping(el) is None:
            parsed = _parse_transformation(el)
            reusable[parsed.name] = parsed

    return [_parse_mapping(el, reusable) for el in root.iter("MAPPING")]


def _enclosing_mapping(el: etree._Element) -> etree._Element | None:
    parent = el.getparent()
    while parent is not None:
        if parent.tag == "MAPPING":
            return parent
        parent = parent.getparent()
    return None


def _parse_mapping(
    el: etree._Element, reusable: dict[str, Transformation] | None = None
) -> Mapping:
    mapping = Mapping(name=el.get("NAME", ""))
    mapping.transformations.update(reusable or {})

    # Mapping-local definitions shadow reusable ones of the same name.
    for tf in el.iter("TRANSFORMATION"):
        parsed = _parse_transformation(tf)
        mapping.transformations[parsed.name] = parsed

    for inst in el.iter("INSTANCE"):
        mapping.instances.append(_parse_instance(inst))

    for conn in el.iter("CONNECTOR"):
        mapping.connectors.append(
            Connector(
                from_instance=conn.get("FROMINSTANCE", ""),
                from_field=conn.get("FROMFIELD", ""),
                to_instance=conn.get("TOINSTANCE", ""),
                to_field=conn.get("TOFIELD", ""),
            )
        )

    return mapping


def _parse_instance(el: etree._Element) -> Instance:
    name = el.get("NAME", "")
    return Instance(
        name=name,
        type=el.get("TYPE", ""),
        transformation_name=el.get("TRANSFORMATION_NAME", name),
        transformation_type=el.get("TRANSFORMATION_TYPE", ""),
    )


def _parse_transformation(el: etree._Element) -> Transformation:
    return Transformation(
        name=el.get("NAME", ""),
        type=el.get("TYPE", ""),
        ports=[_parse_port(f) for f in el.iter("TRANSFORMFIELD")],
        attributes={
            a.get("NAME", ""): a.get("VALUE", "") or "" for a in el.iter("TABLEATTRIBUTE")
        },
    )


def _parse_port(el: etree._Element) -> Port:
    return Port(
        name=el.get("NAME", ""),
        datatype=el.get("DATATYPE", ""),
        porttype=el.get("PORTTYPE", ""),
        expression=el.get("EXPRESSION", "") or "",
        group_by=el.get("GROUPBY", "").upper() in {"YES", "TRUE"},
        sort_key=el.get("SORTKEY", "").upper() in {"YES", "TRUE"},
        sort_descending=el.get("SORTDIRECTION", "").upper().startswith("DESC"),
    )


# --------------------------------------------------------------------------
# Planning: turn the connector graph into an ordered list of CTEs
# --------------------------------------------------------------------------


@dataclass
class Step:
    """One CTE in the generated model."""

    instance: Instance
    cte: str
    type: str


def _snake(text: str) -> str:
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    return text.lower() or "unnamed"


def _topological_order(mapping: Mapping) -> list[Instance]:
    """Kahn's algorithm over the connector graph, stable in document order."""
    order_index = {inst.name: n for n, inst in enumerate(mapping.instances)}
    indegree: dict[str, int] = {inst.name: 0 for inst in mapping.instances}
    edges: dict[str, set[str]] = defaultdict(set)

    for c in mapping.connectors:
        if c.from_instance not in indegree or c.to_instance not in indegree:
            continue
        if c.from_instance == c.to_instance or c.to_instance in edges[c.from_instance]:
            continue
        edges[c.from_instance].add(c.to_instance)
        indegree[c.to_instance] += 1

    ready = sorted(
        (n for n, deg in indegree.items() if deg == 0), key=lambda n: order_index[n]
    )
    ordered: list[str] = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for nxt in sorted(edges[name], key=lambda n: order_index[n]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda n: order_index[n])

    # A cycle (or an instance with no connectors) leaves names unvisited; keep
    # them in document order rather than dropping them from the output.
    placed = set(ordered)
    ordered += [i.name for i in mapping.instances if i.name not in placed]

    by_name = {i.name: i for i in mapping.instances}
    return [by_name[n] for n in ordered]


def build_plan(mapping: Mapping) -> list[Step]:
    """Ordered CTEs for ``mapping``: one per instance that produces rows."""
    steps: list[Step] = []
    used: set[str] = set()

    for inst in _topological_order(mapping):
        if inst.is_target:
            continue
        # A SOURCE feeding a Source Qualifier is folded into that qualifier's
        # CTE, so the ref() appears once rather than twice.
        if inst.is_source and any(
            mapping.type_of(d) == "Source Qualifier"
            for name in mapping.downstream(inst.name)
            if (d := mapping.instance(name))
        ):
            continue

        cte = _unique(_base_cte_name(inst, mapping), used)
        steps.append(Step(instance=inst, cte=cte, type=mapping.type_of(inst)))

    # The terminal step becomes `final`, matching dbt convention.
    if steps and not steps[-1].instance.is_source:
        steps[-1].cte = _unique("final", used - {steps[-1].cte})

    return steps


def _base_cte_name(inst: Instance, mapping: Mapping) -> str:
    if inst.is_source:
        return _snake(inst.transformation_name or inst.name)
    if mapping.type_of(inst) == "Source Qualifier":
        for name in mapping.upstream(inst.name):
            up = mapping.instance(name)
            if up and up.is_source:
                return _snake(up.transformation_name or up.name)
    return _snake(_PREFIX_RE.sub("", inst.name))


def _unique(name: str, used: set[str]) -> str:
    candidate, n = name, 2
    while candidate in used:
        candidate, n = f"{name}_{n}", n + 1
    used.add(candidate)
    return candidate


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def generate_sql(
    mapping: Mapping,
    strict: bool = False,
    source_prefix: str = "stg_",
    lowercase: bool = True,
) -> str:
    """Render ``mapping`` as a dbt model built from chained CTEs.

    With ``strict=True`` an unsupported transformation aborts the conversion
    instead of becoming a pass-through CTE with a TODO comment.
    """
    if strict and mapping.unsupported:
        names = ", ".join(f"{i.name} ({mapping.type_of(i)})" for i in mapping.unsupported)
        raise UnsupportedTransformation(names)

    ctx = _Context(mapping, source_prefix, lowercase)
    steps = build_plan(mapping)

    header = f"-- Generated from PowerCenter mapping: {mapping.name}"
    if not steps:
        return f"{header}\n-- No convertible instances found in this mapping.\n"

    blocks = []
    for n, step in enumerate(steps):
        keyword = "with " if n == 0 else ""
        body = _indent(_render_step(step, ctx))
        note = ctx.notes.pop(step.cte, "")
        blocks.append(f"{note}{keyword}{step.cte} as (\n\n{body}\n\n)")

    return (
        f"{header}\n\n"
        + ",\n\n".join(blocks)
        + f"\n\nselect * from {steps[-1].cte}\n"
    )


class _Context:
    """Shared lookups for the generation pass."""

    def __init__(self, mapping: Mapping, source_prefix: str, lowercase: bool):
        self.mapping = mapping
        self.source_prefix = source_prefix
        self.lowercase = lowercase
        self.cte_of: dict[str, str] = {}
        self.notes: dict[str, str] = {}
        self.current: str = ""  # CTE being rendered, for attributing warnings
        for step in build_plan(mapping):
            self.cte_of[step.instance.name] = step.cte
            # A folded source shares its qualifier's CTE, so columns arriving
            # from the source still resolve to a real name.
            if mapping.type_of(step.instance) == "Source Qualifier":
                for up in mapping.upstream(step.instance.name):
                    inst = mapping.instance(up)
                    if inst and inst.is_source:
                        self.cte_of[up] = step.cte

    def ident(self, name: str) -> str:
        return name.lower() if self.lowercase else name

    def expr(self, expression: str) -> str:
        """Translate an Informatica expression, recording any warnings."""
        sql, warnings = expressions.translate(expression)
        for warning in warnings:
            self.warn(warning)
        return _lower_outside_quotes(sql) if self.lowercase else sql

    def warn(self, message: str) -> None:
        """Attach a TODO to the CTE currently being rendered."""
        if not self.current:
            return
        note = f"-- TODO: {message}\n"
        existing = self.notes.get(self.current, "")
        if note not in existing:
            self.notes[self.current] = existing + note

    def upstream_ctes(self, inst_name: str) -> list[str]:
        out: list[str] = []
        for up in self.mapping.upstream(inst_name):
            cte = self.cte_of.get(up)
            if cte and cte not in out:
                out.append(cte)
        return out

    def origin(self, inst_name: str, port: str) -> tuple[str, str] | None:
        """The (cte, column) a port's value arrives from, via the connectors."""
        for c in self.mapping.connectors:
            if c.to_instance == inst_name and c.to_field == port:
                cte = self.cte_of.get(c.from_instance)
                if cte:
                    return cte, c.from_field
        return None


def _lower_outside_quotes(text: str) -> str:
    """Lowercase everything outside single-quoted literals ('A' stays 'A')."""
    # Odd-indexed segments sit between quotes; rejoin on "'" to keep them.
    return "'".join(
        part if n % 2 else part.lower() for n, part in enumerate(text.split("'"))
    )


def _indent(sql: str) -> str:
    return "\n".join(f"    {line}" if line else "" for line in sql.split("\n"))


def _render_step(step: Step, ctx: _Context) -> str:
    inst = step.instance
    ctx.current = step.cte
    renderers = {
        "Source Qualifier": _render_source,
        "Expression": _render_projection,
        "Filter": _render_filter,
        "Aggregator": _render_aggregator,
        "Joiner": _render_joiner,
        "Sorter": _render_sorter,
    }
    if inst.is_source:
        return _render_source(step, ctx)
    renderer = renderers.get(step.type)
    if renderer is None:
        return _render_passthrough(step, ctx)
    return renderer(step, ctx)


def _render_source(step: Step, ctx: _Context) -> str:
    inst = step.instance
    table = inst.transformation_name or inst.name
    if not inst.is_source:
        for up in ctx.mapping.upstream(inst.name):
            candidate = ctx.mapping.instance(up)
            if candidate and candidate.is_source:
                table = candidate.transformation_name or candidate.name
                break
    ref = f"{ctx.source_prefix}{ctx.ident(table)}"
    return f"select * from {{{{ ref('{ref}') }}}}"


def _render_passthrough(step: Step, ctx: _Context) -> str:
    """Unsupported transformation: keep the chain intact, flag the gap."""
    ctx.notes[step.cte] = (
        f"-- TODO: unsupported transformation {step.instance.name} "
        f"({step.type or 'unknown type'}); rows pass through untransformed\n"
    )
    upstream = ctx.upstream_ctes(step.instance.name)
    source = upstream[0] if upstream else "unknown_upstream"
    return f"select * from {source}"


def _columns(step: Step, ctx: _Context, qualify: bool) -> list[str]:
    """Projection for a transformation, derived from its output ports."""
    tf = ctx.mapping.transformation_for(step.instance)
    if tf is None:
        return ["*"]

    columns: list[str] = []
    for port in tf.ports:
        if not port.is_output:
            continue
        alias = ctx.ident(port.name)
        if port.is_derived:
            columns.append(f"{ctx.expr(port.expression)} as {alias}")
            continue
        origin = ctx.origin(step.instance.name, port.name)
        if qualify and origin:
            cte, column = origin
            reference = f"{cte}.{ctx.ident(column)}"
            columns.append(
                reference if ctx.ident(column) == alias else f"{reference} as {alias}"
            )
        else:
            columns.append(alias)
    return columns or ["*"]


def _from_clause(step: Step, ctx: _Context) -> str:
    upstream = ctx.upstream_ctes(step.instance.name)
    if not upstream:
        return "from unknown_upstream"
    if len(upstream) > 1:
        # Only a Joiner can legally combine pipelines. Anything else with two
        # inputs would produce SQL referencing columns from a CTE that is never
        # joined, so say so instead of emitting silently-broken output.
        ctx.notes[step.cte] = ctx.notes.get(step.cte, "") + (
            f"-- TODO: {step.instance.name} reads from {len(upstream)} pipelines "
            f"({', '.join(upstream)}); only {upstream[0]} is selected from\n"
        )
    return f"from {upstream[0]}"


def _render_projection(step: Step, ctx: _Context) -> str:
    columns = _columns(step, ctx, qualify=False)
    body = ",\n    ".join(columns)
    return f"select\n    {body}\n\n{_from_clause(step, ctx)}"


def _render_filter(step: Step, ctx: _Context) -> str:
    tf = ctx.mapping.transformation_for(step.instance)
    condition = ""
    if tf:
        condition = tf.attribute("Filter Condition")
        if not condition:
            condition = next(
                (
                    p.expression
                    for p in tf.ports
                    if p.name.upper() == "FILTERCONDITION" and p.expression
                ),
                "",
            )

    sql = f"select *\n\n{_from_clause(step, ctx)}"
    if condition:
        sql += f"\n\nwhere {ctx.expr(condition)}"
    return sql


def _render_aggregator(step: Step, ctx: _Context) -> str:
    tf = ctx.mapping.transformation_for(step.instance)
    group_by = [ctx.ident(p.name) for p in tf.ports if p.group_by] if tf else []

    columns = _columns(step, ctx, qualify=False)
    body = ",\n    ".join(columns)
    sql = f"select\n    {body}\n\n{_from_clause(step, ctx)}"
    if group_by:
        sql += "\n\ngroup by " + ", ".join(group_by)
    return sql


def _render_sorter(step: Step, ctx: _Context) -> str:
    tf = ctx.mapping.transformation_for(step.instance)
    keys = []
    if tf:
        keys = [
            ctx.ident(p.name) + (" desc" if p.sort_descending else "")
            for p in tf.ports
            if p.sort_key
        ]
    sql = f"select *\n\n{_from_clause(step, ctx)}"
    if keys:
        sql += "\n\norder by " + ", ".join(keys)
    return sql


def _render_joiner(step: Step, ctx: _Context) -> str:
    tf = ctx.mapping.transformation_for(step.instance)
    upstream = ctx.upstream_ctes(step.instance.name)
    if len(upstream) < 2:
        return _render_projection(step, ctx)

    left, right = upstream[0], upstream[1]
    join_type = _JOIN_TYPES.get(tf.attribute("Join Type") if tf else "", "inner join")
    condition = tf.attribute("Join Condition") if tf else ""
    on = (
        _qualify_condition(ctx.expr(condition), left, right)
        if condition
        else "true /* TODO: no join condition in the export */"
    )

    columns = _columns(step, ctx, qualify=True)
    body = ",\n    ".join(columns)
    return (
        f"select\n    {body}\n\nfrom {left}\n\n{join_type} {right}\n    on {on}"
    )


def _qualify_condition(condition: str, left: str, right: str) -> str:
    """Qualify a bare PowerCenter join condition (``ID = ID``) with CTE names.

    PowerCenter writes join conditions as master/detail port pairs with no table
    qualifier, which is ambiguous in SQL once both sides expose the same column
    name. Pairs already carrying a qualifier are left alone.
    """

    def repl(match: re.Match[str]) -> str:
        return f"{left}.{match.group(1)} = {right}.{match.group(2)}"

    return re.sub(
        r"\b([A-Za-z_][\w]*)\s*=\s*([A-Za-z_][\w]*)\b(?![.\w])", repl, condition
    )


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_models(
    mappings: list[Mapping], out_dir: str | Path, **options
) -> list[Path]:
    """Write one ``<mapping>.sql`` per mapping into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    written = []
    for mapping in mappings:
        path = out / f"{mapping.name}.sql"
        path.write_text(generate_sql(mapping, **options), encoding="utf-8")
        written.append(path)
    return written


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a PowerCenter XML export into dbt SQL models."
    )
    parser.add_argument("xml", help="PowerCenter XML export")
    parser.add_argument(
        "--out", default="models", help="output directory (default: models)"
    )
    parser.add_argument(
        "--source-prefix",
        default="stg_",
        help="prefix for generated ref() names (default: stg_)",
    )
    parser.add_argument(
        "--preserve-case",
        action="store_true",
        help="keep PowerCenter's uppercase identifiers instead of lowercasing",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on unsupported transformations instead of emitting a TODO",
    )
    args = parser.parse_args(argv)

    mappings = parse_file(args.xml)
    if not mappings:
        print(f"No <MAPPING> elements found in {args.xml}", file=sys.stderr)
        return 1

    options = {
        "strict": args.strict,
        "source_prefix": args.source_prefix,
        "lowercase": not args.preserve_case,
    }

    try:
        for path in write_models(mappings, args.out, **options):
            print(f"wrote {path}")
    except UnsupportedTransformation as exc:
        print(f"unsupported transformation(s): {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
