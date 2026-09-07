"""Tests for the PowerCenter -> dbt converter."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import converter  # noqa: E402


EXAMPLE = ROOT / "examples" / "m_customers.xml"


# A compact mapping: source -> qualifier -> expression -> filter -> target,
# with the transformations defined inside the mapping (non-reusable).
SIMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<POWERMART>
  <REPOSITORY NAME="repo">
    <FOLDER NAME="folder">
      <MAPPING NAME="m_customers">
        <TRANSFORMATION NAME="SQ_CUSTOMERS" TYPE="Source Qualifier"/>
        <TRANSFORMATION NAME="EXP_CUSTOMERS" TYPE="Expression">
          <TRANSFORMFIELD NAME="CUSTOMER_ID" PORTTYPE="INPUT/OUTPUT"/>
          <TRANSFORMFIELD NAME="FULL_NAME" PORTTYPE="OUTPUT"
                          EXPRESSION="FIRST_NAME || ' A ' || LAST_NAME"/>
        </TRANSFORMATION>
        <TRANSFORMATION NAME="FIL_ACTIVE" TYPE="Filter">
          <TABLEATTRIBUTE NAME="Filter Condition" VALUE="STATUS = 'A'"/>
        </TRANSFORMATION>

        <INSTANCE NAME="SRC_CUSTOMERS" TYPE="SOURCE" TRANSFORMATION_NAME="customers"/>
        <INSTANCE NAME="SQ_CUSTOMERS" TYPE="TRANSFORMATION"
                  TRANSFORMATION_NAME="SQ_CUSTOMERS" TRANSFORMATION_TYPE="Source Qualifier"/>
        <INSTANCE NAME="EXP_CUSTOMERS" TYPE="TRANSFORMATION"
                  TRANSFORMATION_NAME="EXP_CUSTOMERS" TRANSFORMATION_TYPE="Expression"/>
        <INSTANCE NAME="FIL_ACTIVE" TYPE="TRANSFORMATION"
                  TRANSFORMATION_NAME="FIL_ACTIVE" TRANSFORMATION_TYPE="Filter"/>
        <INSTANCE NAME="TGT_DIM" TYPE="TARGET" TRANSFORMATION_NAME="dim_customers"/>

        <CONNECTOR FROMINSTANCE="SRC_CUSTOMERS" FROMFIELD="CUSTOMER_ID"
                   TOINSTANCE="SQ_CUSTOMERS" TOFIELD="CUSTOMER_ID"/>
        <CONNECTOR FROMINSTANCE="SQ_CUSTOMERS" FROMFIELD="CUSTOMER_ID"
                   TOINSTANCE="EXP_CUSTOMERS" TOFIELD="CUSTOMER_ID"/>
        <CONNECTOR FROMINSTANCE="EXP_CUSTOMERS" FROMFIELD="CUSTOMER_ID"
                   TOINSTANCE="FIL_ACTIVE" TOFIELD="CUSTOMER_ID"/>
        <CONNECTOR FROMINSTANCE="FIL_ACTIVE" FROMFIELD="CUSTOMER_ID"
                   TOINSTANCE="TGT_DIM" TOFIELD="CUSTOMER_ID"/>
      </MAPPING>
    </FOLDER>
  </REPOSITORY>
</POWERMART>
"""

# Instances declared out of execution order, to prove the ordering comes from
# the connector graph rather than document order.
SHUFFLED = """<?xml version="1.0" encoding="UTF-8"?>
<POWERMART>
  <MAPPING NAME="m_shuffled">
    <TRANSFORMATION NAME="EXP_B" TYPE="Expression">
      <TRANSFORMFIELD NAME="V" PORTTYPE="OUTPUT" EXPRESSION="UPPER(V)"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="EXP_A" TYPE="Expression">
      <TRANSFORMFIELD NAME="V" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="SQ_T" TYPE="Source Qualifier"/>

    <INSTANCE NAME="EXP_B" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="EXP_B"
              TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="EXP_A" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="EXP_A"
              TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="SQ_T" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_T"
              TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="SRC_T" TYPE="SOURCE" TRANSFORMATION_NAME="widgets"/>

    <CONNECTOR FROMINSTANCE="SRC_T" FROMFIELD="V" TOINSTANCE="SQ_T" TOFIELD="V"/>
    <CONNECTOR FROMINSTANCE="SQ_T" FROMFIELD="V" TOINSTANCE="EXP_A" TOFIELD="V"/>
    <CONNECTOR FROMINSTANCE="EXP_A" FROMFIELD="V" TOINSTANCE="EXP_B" TOFIELD="V"/>
  </MAPPING>
</POWERMART>
"""

UNSUPPORTED = """<?xml version="1.0" encoding="UTF-8"?>
<POWERMART>
  <MAPPING NAME="m_lookup">
    <TRANSFORMATION NAME="SQ_O" TYPE="Source Qualifier"/>
    <TRANSFORMATION NAME="LKP_RATES" TYPE="Lookup Procedure"/>

    <INSTANCE NAME="SRC_O" TYPE="SOURCE" TRANSFORMATION_NAME="orders"/>
    <INSTANCE NAME="SQ_O" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_O"
              TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="LKP_RATES" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="LKP_RATES"
              TRANSFORMATION_TYPE="Lookup Procedure"/>

    <CONNECTOR FROMINSTANCE="SRC_O" FROMFIELD="ID" TOINSTANCE="SQ_O" TOFIELD="ID"/>
    <CONNECTOR FROMINSTANCE="SQ_O" FROMFIELD="ID" TOINSTANCE="LKP_RATES" TOFIELD="ID"/>
  </MAPPING>
</POWERMART>
"""


@pytest.fixture
def simple():
    return converter.parse_string(SIMPLE)[0]


@pytest.fixture
def example():
    return converter.parse_file(EXAMPLE)[0]


# -- parsing ---------------------------------------------------------------


def test_parses_one_mapping():
    mappings = converter.parse_string(SIMPLE)
    assert [m.name for m in mappings] == ["m_customers"]


def test_parses_sources_and_targets(simple):
    assert simple.sources == ["customers"]
    assert simple.targets == ["dim_customers"]


def test_parses_instances_with_their_types(simple):
    assert simple.type_of(simple.instance("EXP_CUSTOMERS")) == "Expression"
    assert simple.instance("SRC_CUSTOMERS").is_source
    assert simple.instance("TGT_DIM").is_target


def test_parses_ports_and_expressions(simple):
    exp = simple.transformations["EXP_CUSTOMERS"]
    full_name = next(p for p in exp.ports if p.name == "FULL_NAME")
    assert full_name.is_output and full_name.is_derived
    passthrough = next(p for p in exp.ports if p.name == "CUSTOMER_ID")
    assert passthrough.is_output and not passthrough.is_derived


def test_parses_table_attributes_case_insensitively(simple):
    assert simple.transformations["FIL_ACTIVE"].attribute("filter condition") == "STATUS = 'A'"


def test_parses_connectors(simple):
    conn = simple.connectors[0]
    assert (conn.from_instance, conn.to_instance) == ("SRC_CUSTOMERS", "SQ_CUSTOMERS")


def test_resolves_reusable_folder_level_transformations(example):
    # The example defines its transformations outside <MAPPING>; they must still
    # resolve, otherwise every projection degrades to `select *`.
    agg = example.transformation_for(example.instance("AGG_CUSTOMER_ORDERS"))
    assert agg is not None
    assert any(p.group_by for p in agg.ports)


def test_parse_file_matches_parse_string(tmp_path, simple):
    path = tmp_path / "mapping.xml"
    path.write_text(SIMPLE, encoding="utf-8")
    assert converter.parse_file(path)[0] == simple


# -- planning --------------------------------------------------------------


def test_plan_orders_by_connector_graph_not_document_order():
    mapping = converter.parse_string(SHUFFLED)[0]
    assert [s.cte for s in converter.build_plan(mapping)] == ["widgets", "a", "final"]


def test_plan_folds_source_into_its_qualifier(simple):
    ctes = [s.cte for s in converter.build_plan(simple)]
    # One CTE for the source pipeline, not two.
    assert ctes == ["customers", "customers_2", "final"]


def test_plan_excludes_targets(simple):
    assert all(not s.instance.is_target for s in converter.build_plan(simple))


def test_plan_names_terminal_step_final(example):
    assert converter.build_plan(example)[-1].cte == "final"


def test_plan_deduplicates_colliding_cte_names():
    ctes = [s.cte for s in converter.build_plan(converter.parse_string(SIMPLE)[0])]
    assert len(ctes) == len(set(ctes))


# -- generation ------------------------------------------------------------


def test_emits_cte_chain_ending_in_final(simple):
    sql = converter.generate_sql(simple)
    assert sql.count(" as (") == len(converter.build_plan(simple))
    assert sql.startswith("-- Generated from PowerCenter mapping: m_customers")
    assert "with customers as (" in sql
    assert sql.rstrip().endswith("select * from final")


def test_source_qualifier_emits_ref_with_prefix(simple):
    assert "from {{ ref('stg_customers') }}" in converter.generate_sql(simple)
    assert "from {{ ref('customers') }}" in converter.generate_sql(simple, source_prefix="")


def test_expression_emits_derived_columns(simple):
    assert "first_name || ' A ' || last_name as full_name" in converter.generate_sql(simple)


def test_lowercasing_preserves_quoted_literals(simple):
    sql = converter.generate_sql(simple)
    assert "' A '" in sql  # the literal keeps its case
    assert "where status = 'A'" in sql


def test_preserve_case_leaves_identifiers_alone(simple):
    sql = converter.generate_sql(simple, lowercase=False)
    assert "FIRST_NAME || ' A ' || LAST_NAME as FULL_NAME" in sql


def test_filter_emits_where(simple):
    assert "where status = 'A'" in converter.generate_sql(simple)


def test_aggregator_emits_group_by(example):
    sql = converter.generate_sql(example)
    assert "min(order_date) as first_order" in sql
    assert "group by customer_id" in sql


def test_joiner_emits_qualified_join(example):
    sql = converter.generate_sql(example)
    assert "left join customer_orders" in sql
    assert "on customers.customer_id = customer_orders.customer_id" in sql


def test_join_type_maps_from_table_attribute():
    tf = converter.Transformation(name="J", type="Joiner")
    tf.attributes["Join Type"] = "Full Outer Join"
    assert converter._JOIN_TYPES[tf.attribute("Join Type")] == "full outer join"


def test_qualify_condition_leaves_qualified_columns_alone():
    assert (
        converter._qualify_condition("a.id = b.id", "left_cte", "right_cte")
        == "a.id = b.id"
    )


def test_each_cte_selects_from_one_declared_earlier(example):
    """The chain must be self-consistent: no CTE may reference a later one."""
    sql = converter.generate_sql(example)
    declared: list[str] = []
    for step in converter.build_plan(example):
        block = sql.split(f"{step.cte} as (")[1].split("\n)")[0]
        for word in block.replace("\n", " ").split():
            token = word.strip("(),")
            if token in [s.cte for s in converter.build_plan(example)]:
                assert token in declared, f"{step.cte} references {token} before it exists"
        declared.append(step.cte)


def test_unsupported_transformation_passes_rows_through_with_todo():
    mapping = converter.parse_string(UNSUPPORTED)[0]
    sql = converter.generate_sql(mapping)
    assert "-- TODO: unsupported transformation LKP_RATES (Lookup Procedure)" in sql
    assert "select * from orders" in sql  # chain stays intact


def test_strict_mode_raises_on_unsupported():
    mapping = converter.parse_string(UNSUPPORTED)[0]
    with pytest.raises(converter.UnsupportedTransformation, match="LKP_RATES"):
        converter.generate_sql(mapping, strict=True)


def test_multiple_input_pipelines_are_flagged():
    """A non-Joiner reading two pipelines would silently drop one; warn instead."""
    xml = SHUFFLED.replace(
        '<CONNECTOR FROMINSTANCE="EXP_A" FROMFIELD="V" TOINSTANCE="EXP_B" TOFIELD="V"/>',
        '<CONNECTOR FROMINSTANCE="EXP_A" FROMFIELD="V" TOINSTANCE="EXP_B" TOFIELD="V"/>'
        '<CONNECTOR FROMINSTANCE="SQ_T" FROMFIELD="V" TOINSTANCE="EXP_B" TOFIELD="W"/>',
    )
    sql = converter.generate_sql(converter.parse_string(xml)[0])
    assert "reads from 2 pipelines" in sql


def test_mapping_without_convertible_instances():
    sql = converter.generate_sql(converter.Mapping(name="m_empty"))
    assert "No convertible instances found" in sql


# -- output ----------------------------------------------------------------


def test_write_models_creates_one_file_per_mapping(tmp_path, simple):
    written = converter.write_models([simple], tmp_path / "models")
    assert written == [tmp_path / "models" / "m_customers.sql"]
    assert written[0].read_text(encoding="utf-8").startswith("-- Generated from")


def test_cli_writes_models(tmp_path, capsys):
    exit_code = converter.main([str(EXAMPLE), "--out", str(tmp_path / "models")])
    assert exit_code == 0
    assert (tmp_path / "models" / "m_customers.sql").exists()
    assert "wrote" in capsys.readouterr().out


def test_cli_strict_fails_on_unsupported(tmp_path, capsys):
    xml = tmp_path / "lookup.xml"
    xml.write_text(UNSUPPORTED, encoding="utf-8")
    assert converter.main([str(xml), "--out", str(tmp_path / "models"), "--strict"]) == 2
    assert "unsupported transformation" in capsys.readouterr().err


def test_cli_reports_empty_export(tmp_path, capsys):
    xml = tmp_path / "empty.xml"
    xml.write_text("<POWERMART/>", encoding="utf-8")
    assert converter.main([str(xml), "--out", str(tmp_path / "models")]) == 1
    assert "No <MAPPING> elements" in capsys.readouterr().err
