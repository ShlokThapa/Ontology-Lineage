"""What the Jobs page can trigger in Fabric.

Everything here comes from the project's hand-off notes (the Ontology_Lineage chat), not from guessing:

* Set A: the notebooks that are in the workspace today and drive the demo (run order 2, 3, 4, 5).
* Set B: the pipeline-ready notebooks designed afterwards but not imported yet. They show as "not found in
  the workspace" until they are, and then work without any change here.
* The upstream copy pipelines (PostgreSQL -> lakehouse) that the lineage graph describes.
* ``nb_llm_policy_mapping``: the LLM mapping notebook in ``fabric/``.

An item is found in Fabric by EXACT display name. Several names are listed where the item was renamed (the
workspace numbers its Set A notebooks: ``02_policy_docs_chunk_and_map_step2``), and the first that exists wins.
Fabric's Import creates ``<name>(1)`` when the name is taken, so the edited steps 2 and 5 are also listed under
that name, after the exact one: once the ``(1)`` copy is renamed to the real name the exact match wins. A ``_OLD``
copy is never listed.

Never list a name that belongs to a notebook the hand-off says is NOT part of the run, or a button could
trigger the wrong thing: ``02_load_lineage_graph`` (the old loader that inserts synthetic Entity nodes),
``policy_to_metadata_mapping``, ``policy_to_existing_columns_link`` and
``ontology_lineage_extract_metadata_and_chunks``. Meet's lineage extractor is
``01_nb_lineage_extractor_structure_data_step1`` (confirmed); the other candidate, ``nb_lineage_extractor_1``,
is deliberately not matched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NOTEBOOK, PIPELINE = "Notebook", "DataPipeline"
DEFAULT_JOB_TYPE = {NOTEBOOK: "RunNotebook", PIPELINE: "Pipeline"}


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    type: str  # text | bool | int | select
    default: Any = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    key: str
    title: str
    description: str
    item_type: str  # Notebook | DataPipeline
    names: tuple[str, ...]  # Fabric display names to look for, first match wins
    group: str
    params: tuple[Param, ...] = ()
    confirm: str = ""  # non-empty: the run needs an explicit confirmation, and this is the warning shown
    legacy: bool = False
    job_type: str | None = None  # override of DEFAULT_JOB_TYPE

    @property
    def fabric_job_type(self) -> str:
        return self.job_type or DEFAULT_JOB_TYPE[self.item_type]


@dataclass(frozen=True)
class Chain:
    key: str
    title: str
    description: str
    steps: tuple[str, ...]  # job keys, run one after another; the chain stops at the first step that fails
    group: str


# The order here is the order of the flow the page draws: 1 structured data, 2 lineage, 3 policy documents, 4 mapping.
GROUPS = {
    "source": "Structured data harvesting",
    "lineage": "Lineage extraction",
    "policies": "Unstructured data chunking",
    "llm": "Policy document mapping",
    "setb": "Set B (import these first)",
}

# One plain sentence per group for a non-technical reader: what this stage does and why it comes here.
GROUP_NOTES = {
    "source": "Copies the banking tables from the PostgreSQL database into Fabric. This is where all the data starts.",
    "lineage": "Works out where each column comes from and how data moves between tables, and loads that map into the graph.",
    "policies": "Takes the Word policy documents, cuts them into sections and makes them searchable, so a question can find the right clause.",
    "llm": "AI links each policy section to the tables and columns it governs, and you review the result before anything is saved.",
    "setb": "",
}

_LLM_PARAMS = (
    Param("MODE", "Mode", "select", "append", ("append", "overwrite")),
    Param("WRITE", "Write to Neo4j (off = dry run)", "bool", False),
    Param("CONFIRM_OVERWRITE", "Confirm overwrite", "bool", False),
    Param("ONLY_UNLINKED", "Only chunks with no link yet", "bool", False),
    Param("ACCEPT_SELF_VERIFIED", "Accept self-verified links", "bool", False),
    Param("DOCS", "Documents (comma-separated words)", "text", ""),
    Param("LIMIT", "Only the first N chunks (0 = all)", "int", 0),
    Param("WORKERS", "Parallel calls", "int", 4),
)

JOBS: tuple[Job, ...] = (
    # --- Set A: the demo notebooks, strict order 2, 3, 4, 5 ---------------------------------------------------
    Job("policy_chunk", "Split policy documents into sections",
        "Reads every Word policy in Files/Policies and cuts it into sections (chunks), saved in the policy_chunks table.", NOTEBOOK,
        ("02_policy_docs_chunk_and_map_step2", "02_policy_docs_chunk_and_map_step2(1)",
         "policy_docs_chunk_step2", "policy_docs_chunk_and_map_step2"), "policies"),
    Job("policy_load_graph", "Load the sections into the graph",
        "Stores every section in Neo4j and builds the search index (policy_chunk_embedding), so questions find the right clause.", NOTEBOOK,
        ("03_policy_chunks_to_neo4j_vector_step3", "policy_chunks_to_neo4j_vector_step3"), "policies"),
    Job("lineage_embed", "Make tables and columns searchable",
        "Adds search vectors to the tables and columns in the graph and builds lineage_embeddable_index. Run it after the lineage changed.", NOTEBOOK,
        ("04_embed_lineage_nodes_to_neo4j_step4", "embed_lineage_nodes_to_neo4j_step4"), "policies"),
    Job("policy_map_legacy", "Old rule-based mapping (retired)",
        "Retired: the notebook is now a no-op stub (the LLM mapping replaced the register-based mapping). Safe to run; "
        "it reads and writes nothing. If the workspace still has the old version it would write GOVERNS links.",
        NOTEBOOK, ("05_policy_map_to_graph_step5", "05_policy_map_to_graph_step5(1)", "policy_map_to_graph_step5"),
        "policies", legacy=True,
        confirm="Only the retired no-op stub is safe here. If the workspace still has the OLD register-based step 5, this "
                "writes GOVERNS links to Neo4j."),
    # --- the LLM mapping notebook ----------------------------------------------------------------------------
    Job("llm_mapping_nb", "Policy document mapping (notebook)",
        "AI links each policy section to the tables and columns it governs. It only previews the result unless "
        "'Write to Neo4j' is on.", NOTEBOOK, ("nb_llm_policy_mapping",), "llm", params=_LLM_PARAMS),
    # --- lineage extractor: do not modify or rename the notebook -------------------------------------------------
    Job("lineage_extract", "Extract the lineage",
        "Reads the pipelines and column lineage and loads them into Neo4j. The lineage is already loaded, so run it "
        "only when the source changed.", NOTEBOOK, ("01_nb_lineage_extractor_structure_data_step1", "nb_lineage_extractor_step1"), "lineage",
        confirm="This reloads the lineage nodes in Neo4j. Run it only if the source changed."),
    # --- upstream copy pipelines (named in the lineage graph) ---------------------------------------------------
    *(
        Job(key, title, f"Pipeline {name}: copies {src} from PostgreSQL into the lakehouse table {dst}.", PIPELINE, (name,), "source",
            confirm=f"This reloads {dst} in the lakehouse from PostgreSQL.")
        for key, title, name, src, dst in (
            ("pl_accounts", "Copy accounts", "PL_CP_Accounts", "banking.vw_customer_accounts", "dim.account"),
            ("pl_branches", "Copy branches", "PL_CP_Branches", "banking.accounts", "dim.branch"),
            ("pl_customers", "Copy customers (masked)", "PL_CP_Customers_Masked", "banking.customers", "dim.Customer"),
            ("pl_loans", "Copy loan portfolio", "PL_CP_Loan_Portfolio", "banking.loans, customers and branches", "fact.loan_portfolio"),
            ("pl_txn", "Copy monthly transactions", "Pl_CP_Txn_Monthly_Summary", "banking.transactions and accounts", "fact.customer_monthly_txn"),
        )
    ),
    # --- Set B: designed, not imported yet -----------------------------------------------------------------------
    *(
        Job(key, title, desc, NOTEBOOK, (name,), "setb")
        for key, title, name, desc in (
            ("b01", "01 Read table metadata", "01_read_table_metadata", "Reads the lakehouse tables; writes table_metadata and relationship candidates."),
            ("b02", "02 Read policy documents", "02_read_policy_documents", "Reads .docx and digital .pdf policies; writes policy_chunks and quality tables."),
            ("b03", "03 Load policies to graph", "03_load_policies_to_graph", "Loads documents and chunks to Neo4j, embeds changed chunks, builds the vector index."),
            ("b04", "04 Link policies to data", "04_link_policies_to_data", "Links policy text to columns without registers."),
            ("b05", "05 Index graph for search", "05_index_graph_for_search", "Embeds lineage nodes (changed only)."),
            ("b06", "06 Build compliance report", "06_build_compliance_report", "Writes the compliance_* report tables."),
        )
    ),
)

CHAINS: tuple[Chain, ...] = (
    Chain("structured_all", "Refresh all structured data", "Runs the five copy pipelines, one after another.",
          ("pl_accounts", "pl_branches", "pl_customers", "pl_loans", "pl_txn"), "source"),
    Chain("a_demo", "Run all three steps", "Split the policies, load them into the graph, make tables and columns searchable.",
          ("policy_chunk", "policy_load_graph", "lineage_embed"), "policies"),
    Chain("a_policy_changed", "A policy changed", "Split the changed policy again and reload the sections.",
          ("policy_chunk", "policy_load_graph"), "policies"),
    Chain("llm_policy_changed", "A policy changed: split, load, map", "The two steps above, then the policy document mapping.",
          ("policy_chunk", "policy_load_graph", "llm_mapping_nb"), "llm"),
    Chain("llm_everything", "Everything: split, load, prepare, map", "Full refresh ending with the policy document mapping.",
          ("policy_chunk", "policy_load_graph", "lineage_embed", "llm_mapping_nb"), "llm"),
    Chain("b_policies", "Set B: refresh policies (02, 03, 04, 06)", "", ("b02", "b03", "b04", "b06"), "setb"),
    Chain("b_data", "Set B: refresh data (extractor, 01, 05, 04, 06)", "", ("lineage_extract", "b01", "b05", "b04", "b06"), "setb"),
    Chain("b_first_run", "Set B: first run (01, 02, 03, 05, 04, 06)", "The recommended first-run order.",
          ("b01", "b02", "b03", "b05", "b04", "b06"), "setb"),
)

JOBS_BY_KEY = {j.key: j for j in JOBS}
CHAINS_BY_KEY = {c.key: c for c in CHAINS}


def coerce_params(job: Job, supplied: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and type the parameters a caller sent. Unknown names and bad values are refused."""
    specs = {p.name: p for p in job.params}
    out: dict[str, Any] = {}
    for name, value in (supplied or {}).items():
        spec = specs.get(name)
        if spec is None:
            raise ValueError(f"'{name}' is not a parameter of {job.key}.")
        if spec.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false.")
            out[name] = value
        elif spec.type == "int":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100_000:
                raise ValueError(f"{name} must be a whole number between 0 and 100000.")
            out[name] = value
        elif spec.type == "select":
            if value not in spec.options:
                raise ValueError(f"{name} must be one of {', '.join(spec.options)}.")
            out[name] = value
        else:
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError(f"{name} must be text of at most 500 characters.")
            out[name] = value
    return out
