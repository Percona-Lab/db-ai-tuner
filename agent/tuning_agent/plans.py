"""Plans of the top statements (docs/research/var-evidence item 4): what the optimizer settings could act on. An optimizer
flag or limit only matters for a statement whose plan has the choice it governs - a join order, a subquery strategy, an
index merge, a range scan - and the bound on its gain is those statements' share of the time.

For the top statements of a run by time, the sample text performance_schema keeps for the digest (QUERY_SAMPLE_TEXT,
MySQL 8.0.14+) is explained with plain EXPLAIN - never EXPLAIN ANALYZE, which would run it - on a connection of its own
(the statement's schema as default, short lock wait). Prepared statements keep only '?' placeholders and are not explained.
Each plan is reduced to the features the settings act on, and the warnings EXPLAIN raised (3170: the range optimizer ran
out of range_optimizer_max_mem_size)."""
import re

from . import mysqldb

TOP = 10            # statements explained per run
MIN_PCT = 1.0       # of statement time
COVERED_PCT = 80.0  # plans (or plan-less inserts) must cover this share of statement time before anything is called inert
EXPLAINABLE = re.compile(r"^\s*(\(?\s*select|with|update|delete|insert\b.*\bselect\b|replace\b.*\bselect\b)", re.I | re.S)
VALUES_ONLY = re.compile(r"^\s*(insert|replace)\b(?!.*\bselect\b)", re.I | re.S)


def features(rows, text=""):
    """The plan features optimizer settings act on, from tabular EXPLAIN rows (MySQL and MariaDB alike)."""
    f = set()
    tables = [r for r in rows if r.get("table") and not str(r.get("table")).startswith("<union")]
    if len(tables) >= 2:
        f.add("join")
    if len(tables) >= 3:
        f.add("join3")
    for r in rows:
        st, typ = str(r.get("select_type") or "").upper(), str(r.get("type") or "")
        ex, tb = str(r.get("Extra") or ""), str(r.get("table") or "")
        if "SUBQUERY" in st or "MATERIALIZED" in st or any(x in ex for x in ("FirstMatch", "LooseScan", "Start temporary")):
            f.add("subquery")
        if "DERIVED" in st or tb.startswith("<derived"):
            f.add("derived")
        if "UNION" in st:
            f.add("union")
        if typ in ("range", "ref", "ref_or_null", "eq_ref", "index_subquery", "unique_subquery"):
            f.add("lookup")
        if typ in ("range", "ref_or_null"):
            f.add("range")
        if typ in ("ALL", "index"):
            f.add("scan")
        if typ == "index_merge" or len([k for k in str(r.get("possible_keys") or "").split(",") if k]) >= 2:
            f.add("index_choice")  # more than one usable index: index merge and ordering-index choices exist
        for mark, name in (("Using filesort", "sort"), ("Using temporary", "temporary"), ("join buffer", "join_buffer"),
                           ("Using index condition", "icp"), ("Using MRR", "mrr"), ("skip scan", "skip_scan"),
                           ("Rowid-ordered scan", "mrr"), ("Using index for group-by", "group_by_index")):
            if mark in ex:
                f.add(name)
    if re.search(r"\border\s+by\b", text, re.I) and re.search(r"\blimit\b", text, re.I):
        f.add("order_limit")
    if re.search(r"\b(in|exists)\s*\(\s*select\b", text, re.I):
        f.add("subquery")
    return f


def collect(cfg, top_statements, variables):
    """[{digest, pct_time, query, features, warnings, plan | error}] for the run's top statements."""
    stmts = [t for t in (top_statements or []) if (t.get("pct_time") or 0) >= MIN_PCT][:TOP]
    if not stmts:
        return []
    fl, ver = mysqldb.flavor_of(variables or {})[:2]
    db = mysqldb.DB(cfg)
    out = []
    try:
        db.connect()
        db.execute("SET SESSION lock_wait_timeout = 2")
        samples = {}
        digests = [t["digest"] for t in stmts if not t.get("prepared")]
        if digests and not (fl == "mariadb"):
            try:
                for schema, dg, txt in db.rows(
                        "SELECT SCHEMA_NAME, DIGEST, QUERY_SAMPLE_TEXT FROM performance_schema.events_statements_summary_by_digest "
                        "WHERE DIGEST IN (%s)" % ",".join(["%s"] * len(digests)), tuple(digests)):
                    samples[(schema or "", dg)] = txt
            except Exception:  # noqa - an older release without sample texts
                pass
        maxlen = int((variables or {}).get("performance_schema_max_sql_text_length") or 1024)
        for t in stmts:
            rec = {"digest": t["digest"], "pct_time": t.get("pct_time"), "query": (t.get("query") or "")[:200]}
            txt = samples.get((t.get("schema") or "", t["digest"]))
            if VALUES_ONLY.match(t.get("query") or ""):
                rec["features"] = []  # INSERT ... VALUES: no plan to choose
                out.append(rec)
                continue
            if t.get("prepared") or not txt:
                rec["error"] = "no sample text (prepared statement)" if t.get("prepared") else "no sample text"
                out.append(rec)
                continue
            if len(txt) >= maxlen or not EXPLAINABLE.match(txt):
                rec["error"] = "sample text truncated" if len(txt) >= maxlen else "not explainable"
                out.append(rec)
                continue
            try:
                if t.get("schema"):
                    db.execute("USE `%s`" % str(t["schema"]).replace("`", "``"))
                rows = db.dict_rows("EXPLAIN " + txt)
                warns = [int(w[1]) for w in db.rows("SHOW WARNINGS") if int(w[1]) != 1003]
                rec["features"] = sorted(features(rows, txt))
                rec["warnings"] = warns
                rec["plan"] = [{k: r.get(k) for k in ("id", "select_type", "table", "type", "possible_keys", "key", "rows", "filtered", "Extra")}
                               for r in rows][:20]
            except Exception as e:  # noqa
                rec["error"] = str(e)[:200]
            out.append(rec)
    except Exception as e:  # noqa - never fail a run for its plans
        return [{"error": str(e)[:200]}]
    finally:
        db.close()
    return out


# what each optimizer setting chooses between, as plan features (any one present: the setting can matter)
FLAG_NEEDS = {
    "index_merge": {"index_choice"}, "index_merge_union": {"index_choice"}, "index_merge_sort_union": {"index_choice"},
    "index_merge_intersection": {"index_choice"}, "index_condition_pushdown": {"lookup", "icp"}, "mrr": {"range", "mrr"},
    "mrr_cost_based": {"range", "mrr"}, "block_nested_loop": {"join"}, "batched_key_access": {"join"}, "hash_join": {"join"},
    "condition_fanout_filter": {"join"}, "semijoin": {"subquery"}, "materialization": {"subquery"}, "firstmatch": {"subquery"},
    "loosescan": {"subquery"}, "duplicateweedout": {"subquery"}, "subquery_materialization_cost_based": {"subquery"},
    "subquery_to_derived": {"subquery"}, "derived_merge": {"derived"}, "derived_condition_pushdown": {"derived"},
    "use_index_extensions": {"lookup", "sort", "order_limit"}, "skip_scan": {"scan", "range"},
    "prefer_ordering_index": {"order_limit"}, "engine_condition_pushdown": set(),
    # MariaDB names
    "extended_keys": {"lookup", "sort", "order_limit"}, "join_cache_incremental": {"join"}, "join_cache_hashed": {"join"},
    "join_cache_bka": {"join"}, "outer_join_with_cache": {"join"}, "semijoin_with_cache": {"subquery"},
    "optimize_join_buffer_size": {"join"}, "table_elimination": {"join"}, "in_to_exists": {"subquery"},
    "exists_to_in": {"subquery"}, "partial_match_rowid_merge": {"subquery"}, "partial_match_table_scan": {"subquery"},
    "split_materialized": {"derived"}, "condition_pushdown_for_derived": {"derived"},
    "condition_pushdown_for_subquery": {"subquery"}, "orderby_uses_equalities": {"sort", "order_limit"},
    "rowid_filter": {"lookup"}, "not_null_range_scan": {"join"},
}
LIMIT_NEEDS = {"optimizer_search_depth": {"join3"}, "optimizer_prune_level": {"join3"}, "eq_range_index_dive_limit": {"range"},
               "range_optimizer_max_mem_size": {"range", "index_choice"}}


def coverage(plans):
    """Share of statement time whose plan (or plan-less insert) is known, and the features over those statements."""
    pct, feats = 0.0, set()
    for p in plans or []:
        if "features" in p:
            pct += p.get("pct_time") or 0
            feats |= set(p["features"])
    return pct, feats


def inert(var, val, plans):
    """Why an optimizer setting cannot matter for these statements, or None: no explained statement (covering at least
    COVERED_PCT of the statement time) has a plan choice the setting governs."""
    pct, feats = coverage(plans)
    if pct < COVERED_PCT:
        return None
    if var == "optimizer_switch":
        flags = {f.split("=")[0].strip() for f in str(val or "").split(",") if "=" in f}
        if not flags or any(f not in FLAG_NEEDS or FLAG_NEEDS[f] & feats for f in flags):
            return None
        return (f"the top statements ({pct:.0f}% of statement time) have no plan choice {', '.join(sorted(flags))} governs "
                f"(plan features: {', '.join(sorted(feats)) or 'single-table lookups and inserts only'})")
    need = LIMIT_NEEDS.get(var)
    if need is None or need & feats:
        return None
    return f"the top statements ({pct:.0f}% of statement time) have no plan choice {var} governs"
