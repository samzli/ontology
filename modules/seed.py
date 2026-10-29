"""Idempotent first-run seed migration.

Materializes the legacy hard-coded VNB/APE ontology into a fully data-driven
**built-in sample pack** inside SQLite. The platform is generic — this pack is
just the shipped example (and can be reset / exported / copied in the admin
portal).

Also imports a default model from the provider-neutral ``.env`` bootstrap
(``LLM_*`` variables; legacy ``SILICONFLOW_*`` still supported) into the
``model_configs`` table exactly once.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.db import (
    BASE_DIR,
    audit,
    encrypt_secret,
    get_conn,
    get_setting,
    jdumps,
    now,
    set_setting,
)

PACK_SLUG = "vnb-ape-sample"
PACK_NAME = "VNB / APE Insurance Sample Pack"
PACK_DESC = (
    "Built-in FY2025 sample: company VNB / APE / margin, four insurance "
    "products, three distribution channels, twenty agents, channel-product "
    "mix, the reconciliation gap and its attribution graph."
)


# ===========================================================================
# Graph definition — 14 concept nodes
# ===========================================================================

NODES: List[Dict[str, Any]] = [
    {
        "key": "TotalVNB",
        "grp": "financial",
        "core": 1,
        "value": "$11.08M",
        "desc": "Top-level financial KPI. Stated total Value of New Business for 2025. "
        "Identity: TotalVNB = Σ_channel (ChannelAPE × ChannelBlendedMargin).",
        "aliases": [
            "total vnb",
            "overall vnb",
            "what is vnb",
            "value of new business",
            "总vnb",
            "整体vnb",
            "新业务价值",
            "vnb总额",
        ],
    },
    {
        "key": "TotalAPE",
        "grp": "financial",
        "core": 1,
        "value": "$68.00M",
        "desc": "Total Annual Premium Equivalent for 2025. Structural aggregate of ChannelAPE.",
        "aliases": [
            "total ape",
            "overall ape",
            "what is ape",
            "premium equivalent",
            "总ape",
            "整体ape",
            "保费当量",
            "ape总额",
        ],
    },
    {
        "key": "BlendedVNBMargin",
        "grp": "financial",
        "core": 1,
        "value": "16.3%",
        "desc": "Company-level VNB margin (Total VNB ÷ Total APE). Weighted average of channel blended margins.",
        "aliases": [
            "overall margin",
            "blended margin",
            "overall vnb margin",
            "整体边际",
            "整体vnb margin",
            "综合边际",
        ],
    },
    {
        "key": "ChannelAPE",
        "grp": "financial",
        "core": 1,
        "value": "$20 / $30 / $18M",
        "desc": "Per-channel APE. Identity: ChannelAPE = agent_count × avg_productivity.",
        "aliases": [
            "channel ape",
            "渠道ape",
            "渠道保费",
            "channel:agency",
            "channel:banca",
            "channel:brokerage",
        ],
    },
    {
        "key": "ChannelBlendedMargin",
        "grp": "financial",
        "core": 1,
        "value": "18.1% / 21.9% / 17.2%",
        "desc": "Per-channel blended VNB margin. Identity: Σ_product (mix_weight × product_vnb_margin).",
        "aliases": [
            "channel blended",
            "highest margin",
            "blended vnb",
            "渠道边际",
            "渠道margin",
            "channel:agency",
            "channel:banca",
            "channel:brokerage",
        ],
    },
    {
        "key": "AgentCount",
        "grp": "operational",
        "core": 0,
        "value": "500 / 200 / 80",
        "desc": "Number of active agents per channel (Agency / Banca / Brokerage).",
        "aliases": [
            "agent count",
            "most agents",
            "number of agents",
            "headcount",
            "代理人数量",
            "代理人人数",
            "代理人最多",
            "多少代理人",
            "人力",
            "channel:agency",
            "channel:banca",
            "channel:brokerage",
        ],
    },
    {
        "key": "AgentProductivity",
        "grp": "operational",
        "core": 0,
        "value": "$40K / $150K / $225K",
        "desc": "Average APE productivity per agent in each channel.",
        "aliases": ["productivity", "productive", "产能", "人均", "生产力"],
    },
    {
        "key": "AgentChurnRate",
        "grp": "operational",
        "core": 0,
        "value": "30% / 10% / 15%",
        "desc": "Channel-level agent churn rate; reduces effective agent count.",
        "aliases": ["churn rate", "highest churn", "流失率", "脱落率"],
    },
    {
        "key": "ProductMixWeight",
        "grp": "mix",
        "core": 0,
        "value": "per channel",
        "desc": "Share of channel APE by product; each channel's weights sum to 100%.",
        "aliases": [
            "product mix",
            "mix weight",
            "dominant product",
            "sales composition",
            "产品组合",
            "组合占比",
            "业务组合",
            "占比",
            "主导产品",
            "product:termlife",
            "product:participatinglife",
            "product:unitlinked",
            "product:universallife",
        ],
    },
    {
        "key": "ProductVNBMargin",
        "grp": "mix",
        "core": 0,
        "value": "10% / 18% / 25% / 15%",
        "desc": "Per-product VNB conversion margin (Term Life / Participating / Unit-Linked / Universal Life).",
        "aliases": [
            "product margin",
            "highest-margin",
            "highest margin product",
            "lowest-margin",
            "product vnb",
            "产品边际",
            "边际最高",
            "边际最低",
            "product:termlife",
            "product:participatinglife",
            "product:unitlinked",
            "product:universallife",
        ],
    },
    {
        "key": "Certification",
        "grp": "cross",
        "core": 0,
        "value": "5 levels",
        "desc": "Agent certification: Term Only / Participating / Unit-Linked / Universal Life / All Products.",
        "aliases": [
            "certification",
            "certified",
            "all products",
            "认证",
            "资质",
            "持证",
        ],
    },
    {
        "key": "ProductMixEligibility",
        "grp": "cross",
        "core": 0,
        "value": "gated",
        "desc": "Which products an agent is allowed to sell; gated by Certification (edge E9).",
        "aliases": [
            "eligibility",
            "eligible",
            "can sell",
            "gate",
            "allowed to sell",
            "可销售",
            "销售资格",
            "门槛",
        ],
    },
    {
        "key": "ChurnRisk",
        "grp": "cross",
        "core": 0,
        "value": "Low/Med/High",
        "desc": "Forward-looking agent-level churn risk; feeds AgentCount (edge E10).",
        "aliases": [
            "churn risk",
            "high churn",
            "at risk",
            "流失风险",
            "高流失",
            "风险代理人",
        ],
    },
    {
        "key": "CommissionExpense",
        "grp": "cross",
        "core": 0,
        "value": "15-25%",
        "desc": "Commission and expense loads that influence product VNB margin (edge E11).",
        "aliases": ["commission", "expense", "佣金", "费用"],
    },
    # ---- causal KPI-linkage chain (Agency performance -> financial) ----
    {
        "key": "AgentStatus",
        "grp": "operational",
        "core": 0,
        "value": "Active / Inactive",
        "desc": "Each agent's monthly status. COUNT(agents where status=Active) gives the "
        "active salesforce for that month.",
        "aliases": [
            "agent status",
            "active status",
            "inactive",
            "活跃状态",
            "在职状态",
        ],
    },
    {
        "key": "ActiveAgentCount",
        "grp": "operational",
        "core": 1,
        "value": "active agents",
        "desc": "Number of active agents this month (latest month across all channels). "
        "Together with average productivity it sets Agency APE.",
        "aliases": [
            "active agents",
            "active count",
            "active salesforce",
            "活跃代理人",
            "活跃人数",
        ],
    },
    {
        "key": "AgencyAPE",
        "grp": "financial",
        "core": 1,
        "value": "Agency APE",
        "desc": "Agency-channel Annual Premium Equivalent. Identity: Active Agents x Avg Productivity.",
        "aliases": ["agency ape", "agency production", "经代ape", "代理人渠道ape"],
    },
    {
        "key": "WeightedVNBMargin",
        "grp": "mix",
        "core": 0,
        "value": "weighted margin",
        "desc": "Agency weighted VNB margin = SUM(product mix x product VNB margin).",
        "aliases": ["weighted vnb margin", "agency margin", "加权margin", "加权边际"],
    },
    {
        "key": "AgencyVNB",
        "grp": "financial",
        "core": 1,
        "value": "Agency VNB",
        "desc": "Agency-channel VNB = Agency APE x weighted VNB margin; contributes to Total VNB.",
        "aliases": ["agency vnb", "agency contribution", "经代vnb", "代理人渠道vnb"],
    },
    {
        "key": "TargetTotalVNB",
        "grp": "financial",
        "core": 0,
        "value": "management target",
        "desc": "A forward financial target (e.g. double next-month VNB). Back-solves the "
        "operational levers required to reach it.",
        "aliases": ["target vnb", "goal", "double vnb", "目标vnb", "翻倍", "业绩目标"],
    },
    {
        "key": "OperationalLevers",
        "grp": "cross",
        "core": 0,
        "value": "Agents · Productivity · Mix",
        "desc": "The operational levers management can pull to hit a VNB target: active-agent "
        "count, per-agent productivity and product mix.",
        "aliases": [
            "levers",
            "actions",
            "what has to change",
            "运营杠杆",
            "管理动作",
            "抓手",
        ],
    },
]

# Multi-input edges use comma-separated source keys and are expanded at load.
EDGES: List[Dict[str, Any]] = [
    {
        "key": "E1",
        "src": "ChannelAPE",
        "tgt": "TotalAPE",
        "type": "STRUCTURAL",
        "label": "Σ",
        "desc": "Structural aggregation: channel APE rolls up to Total APE (Σ).",
    },
    {
        "key": "E2",
        "src": "ChannelBlendedMargin",
        "tgt": "BlendedVNBMargin",
        "type": "STRUCTURAL",
        "label": "weighted avg",
        "desc": "Structural aggregation: channel blended margins roll up to the company Blended VNB Margin (APE-weighted average).",
    },
    {
        "key": "E3",
        "src": "TotalAPE,BlendedVNBMargin",
        "tgt": "TotalVNB",
        "type": "STRUCTURAL",
        "label": "APE × Margin",
        "desc": "Core identity: Total VNB = Total APE × Blended VNB Margin.",
    },
    {
        "key": "E4",
        "src": "AgentCount",
        "tgt": "ChannelAPE",
        "type": "OPERATIONAL",
        "label": "× avg productiv.",
        "desc": "Operational driver: Agent Count × average productivity produces Channel APE.",
    },
    {
        "key": "E5",
        "src": "AgentProductivity",
        "tgt": "ChannelAPE",
        "type": "OPERATIONAL",
        "label": "× agent count",
        "desc": "Operational driver: Agent Productivity × agent count produces Channel APE.",
    },
    {
        "key": "E6",
        "src": "AgentChurnRate",
        "tgt": "AgentCount",
        "type": "OPERATIONAL",
        "label": "−churn",
        "desc": "Operational driver: churn reduces the effective Agent Count.",
    },
    {
        "key": "E7",
        "src": "ProductMixWeight",
        "tgt": "ChannelBlendedMargin",
        "type": "MIX",
        "label": "Σ weight×margin",
        "desc": "Mix driver: product-mix weights feed the channel blended margin (Σ weight × margin).",
    },
    {
        "key": "E8",
        "src": "ProductVNBMargin",
        "tgt": "ChannelBlendedMargin",
        "type": "MIX",
        "label": "weight×margin",
        "desc": "Mix driver: product VNB margins, weighted by mix, compose the channel blended margin.",
    },
    {
        "key": "E9",
        "src": "Certification",
        "tgt": "ProductMixEligibility",
        "type": "CROSS_LINK",
        "label": "gates",
        "desc": "Cross-link: Certification gates Product-Mix Eligibility — Term Only agents cannot sell Unit-Linked.",
    },
    {
        "key": "E10",
        "src": "ChurnRisk",
        "tgt": "AgentCount",
        "type": "CROSS_LINK",
        "label": "feeds forward",
        "desc": "Cross-link: agent Churn Risk feeds forward into Agent Count.",
    },
    {
        "key": "E11",
        "src": "CommissionExpense",
        "tgt": "ProductVNBMargin",
        "type": "CROSS_LINK",
        "label": "influences",
        "desc": "Cross-link: Commission / Expense loads influence the product VNB margin.",
    },
    # ---- explicit KPI-linkage causal chain ----
    {
        "key": "E12",
        "src": "AgentStatus",
        "tgt": "ActiveAgentCount",
        "type": "OPERATIONAL",
        "label": "COUNT active",
        "desc": "KPI Linkage: Agent Status aggregates_to Active Agent Count = COUNT(agents where status=Active).",
    },
    {
        "key": "E13",
        "src": "ActiveAgentCount",
        "tgt": "AgencyAPE",
        "type": "OPERATIONAL",
        "label": "x avg productivity",
        "desc": "KPI Linkage: Active Agent Count drives Agency APE (Active Agents x Avg Productivity).",
    },
    {
        "key": "E14",
        "src": "AgentProductivity",
        "tgt": "AgencyAPE",
        "type": "OPERATIONAL",
        "label": "x active agents",
        "desc": "KPI Linkage: Agent Productivity drives Agency APE (average APE per active agent).",
    },
    {
        "key": "E15",
        "src": "ProductMixWeight",
        "tgt": "WeightedVNBMargin",
        "type": "MIX",
        "label": "determines",
        "desc": "KPI Linkage: Product Mix determines Weighted VNB Margin = SUM(Mix x Product Margin).",
    },
    {
        "key": "E16",
        "src": "ProductVNBMargin",
        "tgt": "WeightedVNBMargin",
        "type": "MIX",
        "label": "weighted",
        "desc": "KPI Linkage: per-product VNB margins combine with mix into the weighted margin.",
    },
    {
        "key": "E17",
        "src": "AgencyAPE",
        "tgt": "AgencyVNB",
        "type": "STRUCTURAL",
        "label": "x weighted margin",
        "desc": "KPI Linkage: Agency APE contributes_to Agency VNB (Agency APE x Weighted Margin).",
    },
    {
        "key": "E18",
        "src": "WeightedVNBMargin",
        "tgt": "AgencyVNB",
        "type": "STRUCTURAL",
        "label": "APE x margin",
        "desc": "KPI Linkage: weighted margin converts Agency APE into Agency VNB.",
    },
    {
        "key": "E19",
        "src": "AgencyVNB",
        "tgt": "TotalVNB",
        "type": "STRUCTURAL",
        "label": "contributes",
        "desc": "KPI Linkage: Agency VNB contributes_to Total VNB (sum across channels).",
    },
    {
        "key": "E20",
        "src": "TargetTotalVNB",
        "tgt": "OperationalLevers",
        "type": "CROSS_LINK",
        "label": "requires",
        "desc": "KPI Linkage: Target Total VNB requires Operational Levers (back-solve agents / productivity / mix).",
    },
    {
        "key": "E21",
        "src": "OperationalLevers",
        "tgt": "ActiveAgentCount",
        "type": "CROSS_LINK",
        "label": "lever: headcount",
        "desc": "Management lever: add/retain active agents to close the gap to target VNB.",
    },
    {
        "key": "E22",
        "src": "OperationalLevers",
        "tgt": "AgentProductivity",
        "type": "CROSS_LINK",
        "label": "lever: productivity",
        "desc": "Management lever: lift per-agent productivity to close the gap to target VNB.",
    },
]

# ===========================================================================
# v2 upgrade — monthly data-table entities. Bridges the 5-class formal
# schema to the actual knowledge-base tables (VNB Monthly, Channel Monthly,
# Agent Monthly, Product Mix Monthly, Scenario Analysis) so the ontology,
# the class graph and the lineage view all match the Knowledge library.
# ===========================================================================

UPGRADE_ENTITIES: List[Dict[str, Any]] = [
    {
        "key": "vnb_monthly",
        "label": "VNB Monthly Results",
        "layer": 1,
        "desc": "Company-level monthly financial results: total APE, VNB, margin "
        "and the Agency contribution series. Source table: VNB Monthly (company).",
        "attrs": [
            ("month", "Month", "STRING", ""),
            ("total_ape", "Total APE", "FLOAT", "$M"),
            ("total_vnb", "Total VNB", "FLOAT", "$M"),
            ("total_margin", "VNB Margin", "FLOAT", "%"),
            ("agency_ape", "Agency APE", "FLOAT", "$M"),
            ("agency_vnb", "Agency VNB", "FLOAT", "$M"),
            ("agency_share", "Agency share of VNB", "FLOAT", "%"),
            ("mom_vnb", "MoM VNB change", "FLOAT", "%"),
        ],
    },
    {
        "key": "channel_monthly",
        "label": "Channel Monthly KPIs",
        "layer": 2,
        "desc": "Monthly operational and financial KPIs per distribution channel: "
        "active agents, productivity, APE, weighted margin and VNB.",
        "attrs": [
            ("month", "Month", "STRING", ""),
            ("channel_id", "Channel ID", "STRING", ""),
            ("active", "Active agents", "INTEGER", ""),
            ("avg_prod", "Avg productivity", "FLOAT", "$k"),
            ("ape", "APE", "FLOAT", "$M"),
            ("margin", "Weighted VNB margin", "FLOAT", "%"),
            ("vnb", "VNB", "FLOAT", "$M"),
        ],
    },
    {
        "key": "agent_monthly",
        "label": "Agent Monthly Performance",
        "layer": 3,
        "desc": "Monthly performance of every Agency agent: status, productivity, "
        "APE, tenure, certification and churn risk.",
        "attrs": [
            ("month", "Month", "STRING", ""),
            ("agent_id", "Agent ID", "STRING", ""),
            ("channel_id", "Channel ID", "STRING", ""),
            ("status", "Status", "STRING", ""),
            ("prod", "Productivity", "FLOAT", "$k"),
            ("ape", "APE", "FLOAT", "$M"),
            ("churn_risk", "Churn risk", "STRING", ""),
        ],
    },
    {
        "key": "product_mix_monthly",
        "label": "Product Mix Monthly",
        "layer": 3,
        "desc": "Monthly product mix per channel: each product's weight, its VNB "
        "margin and the weighted-margin contribution.",
        "attrs": [
            ("month", "Month", "STRING", ""),
            ("channel_id", "Channel ID", "STRING", ""),
            ("product_id", "Product ID", "STRING", ""),
            ("mix", "Product mix", "FLOAT", "%"),
            ("margin", "VNB margin", "FLOAT", "%"),
            ("weighted", "Weighted margin component", "FLOAT", "%"),
        ],
    },
    {
        "key": "scenario_analysis",
        "label": "Scenario Analysis",
        "layer": 4,
        "desc": "Reference back-solve from a financial VNB target to the "
        "operational levers (agents, productivity, mix) required to reach it.",
        "attrs": [
            ("item", "Scenario / input", "STRING", ""),
            ("value", "Value", "STRING", ""),
            ("unit", "Unit / interpretation", "STRING", ""),
        ],
    },
]

UPGRADE_RELATIONS: List[Dict[str, Any]] = [
    {
        "key": "channel_monthly_aggregates_vnb_monthly",
        "label": "aggregates to company month",
        "card": "MANY_TO_ONE",
        "src": "channel_monthly",
        "tgt": "vnb_monthly",
        "src_attr": "month",
        "tgt_attr": "month",
        "desc": "Summing channel-month VNB (and APE) across channels yields the "
        "company VNB Monthly row for that month.",
    },
    {
        "key": "channel_monthly_describes_channel",
        "label": "monthly KPIs of",
        "card": "MANY_TO_ONE",
        "src": "channel_monthly",
        "tgt": "distribution_channel",
        "src_attr": "channel_id",
        "tgt_attr": "id",
        "desc": "Each Channel Monthly row carries the KPIs of one distribution " "channel for one month.",
    },
    {
        "key": "agent_monthly_describes_agent",
        "label": "monthly performance of",
        "card": "MANY_TO_ONE",
        "src": "agent_monthly",
        "tgt": "agent",
        "src_attr": "agent_id",
        "tgt_attr": "id",
        "desc": "Each Agent Monthly row is one agent's performance in one month.",
    },
    {
        "key": "agent_monthly_rolls_to_channel_monthly",
        "label": "rolls up to",
        "card": "MANY_TO_ONE",
        "src": "agent_monthly",
        "tgt": "channel_monthly",
        "src_attr": "channel_id",
        "tgt_attr": "channel_id",
        "desc": "Agent monthly APE aggregates (per month) into the channel monthly " "APE and active-agent counts.",
    },
    {
        "key": "product_mix_monthly_sells_product",
        "label": "sales mix of",
        "card": "MANY_TO_ONE",
        "src": "product_mix_monthly",
        "tgt": "insurance_product",
        "src_attr": "product_id",
        "tgt_attr": "id",
        "desc": "Each Product Mix Monthly row references one insurance product's "
        "share and margin in a channel-month.",
    },
    {
        "key": "product_mix_monthly_in_channel_monthly",
        "label": "within channel month",
        "card": "MANY_TO_ONE",
        "src": "product_mix_monthly",
        "tgt": "channel_monthly",
        "src_attr": "channel_id",
        "tgt_attr": "channel_id",
        "desc": "Product-mix rows belong to a channel-month; weights sum to 100% " "within each channel and month.",
    },
    {
        "key": "vnb_monthly_feeds_vnb_summary",
        "label": "monthly actuals feed",
        "card": "ONE_TO_MANY",
        "src": "vnb_monthly",
        "tgt": "vnb_summary",
        "src_attr": "month",
        "tgt_attr": "month",
        "desc": "The monthly result series feeds the VNB Summary reporting entity "
        "(latest month / year-to-date views).",
    },
    {
        "key": "scenario_analysis_targets_vnb_summary",
        "label": "back-solves targets for",
        "card": "MANY_TO_ONE",
        "src": "scenario_analysis",
        "tgt": "vnb_summary",
        "src_attr": "item",
        "tgt_attr": "month",
        "desc": "Scenario analysis back-solves from a target VNB Summary outcome " "to the operational levers.",
    },
]

# Graph nodes for the new entities (values filled by _graph_node_value_overrides)
UPGRADE_ENTITY_NODES: List[Dict[str, Any]] = [
    {
        "key": "VnbMonthly",
        "grp": "financial",
        "core": 1,
        "value": "monthly series",
        "desc": "Company VNB Monthly table — the actual monthly result series "
        "(APE, VNB, margin, Agency contribution) that the KPI chain aggregates.",
        "aliases": ["vnb monthly", "monthly vnb", "monthly results", "月度vnb"],
    },
    {
        "key": "ChannelMonthly",
        "grp": "operational",
        "core": 1,
        "value": "channel KPIs",
        "desc": "Channel Monthly KPIs table — per-channel monthly active agents, "
        "productivity, APE, weighted margin and VNB.",
        "aliases": ["channel monthly", "channel kpis", "渠道月度"],
    },
    {
        "key": "AgentMonthly",
        "grp": "operational",
        "core": 0,
        "value": "agent rows",
        "desc": "Agent Monthly Performance table — every agent's monthly status, "
        "productivity and APE; the lowest-grain operational data.",
        "aliases": ["agent monthly", "agent performance", "代理人月度"],
    },
    {
        "key": "ProductMixMonthly",
        "grp": "mix",
        "core": 0,
        "value": "mix by month",
        "desc": "Product Mix Monthly table — per channel and month, each "
        "product's mix weight and VNB margin contribution.",
        "aliases": ["product mix monthly", "monthly mix", "月度产品组合"],
    },
    {
        "key": "ScenarioAnalysis",
        "grp": "cross",
        "core": 0,
        "value": "target back-solve",
        "desc": "Scenario Analysis table — reference back-solve from a VNB "
        "target to the required operational levers.",
        "aliases": ["scenario", "scenario analysis", "情景分析"],
    },
]

# Edges wiring the entity nodes into the existing KPI causal chain
UPGRADE_GRAPH_EDGES: List[Dict[str, Any]] = [
    {
        "key": "X1",
        "src": "VnbMonthly",
        "tgt": "TotalVNB",
        "type": "STRUCTURAL",
        "label": "monthly actuals",
        "desc": "The VNB Monthly table holds the actual monthly Total VNB figures "
        "that realise the TotalVNB KPI each month.",
    },
    {
        "key": "X2",
        "src": "VnbMonthly",
        "tgt": "AgencyVNB",
        "type": "STRUCTURAL",
        "label": "agency series",
        "desc": "The agency_ape / agency_vnb columns of VNB Monthly are the " "Agency contribution series over time.",
    },
    {
        "key": "X3",
        "src": "ChannelMonthly",
        "tgt": "ChannelAPE",
        "type": "OPERATIONAL",
        "label": "channel monthly APE",
        "desc": "Channel Monthly KPIs measures each channel's APE every month.",
    },
    {
        "key": "X4",
        "src": "ChannelMonthly",
        "tgt": "ChannelBlendedMargin",
        "type": "MIX",
        "label": "channel weighted margin",
        "desc": "Channel Monthly carries each channel's weighted VNB margin.",
    },
    {
        "key": "X5",
        "src": "AgentMonthly",
        "tgt": "AgentStatus",
        "type": "OPERATIONAL",
        "label": "monthly status rows",
        "desc": "Agent Monthly records each agent's Active/Inactive status per "
        "month — the raw input to the status KPI.",
    },
    {
        "key": "X6",
        "src": "AgentMonthly",
        "tgt": "ActiveAgentCount",
        "type": "OPERATIONAL",
        "label": "COUNT active",
        "desc": "COUNT(Agent Monthly rows where status=Active) gives the monthly " "Active Agent Count.",
    },
    {
        "key": "X7",
        "src": "ProductMixMonthly",
        "tgt": "ProductMixWeight",
        "type": "MIX",
        "label": "monthly mix weights",
        "desc": "Product Mix Monthly holds each channel's product weights over "
        "time — the time series behind ProductMixWeight.",
    },
    {
        "key": "X8",
        "src": "ScenarioAnalysis",
        "tgt": "TargetTotalVNB",
        "type": "CROSS_LINK",
        "label": "target input",
        "desc": "Scenario Analysis supplies the target VNB and the back-solved " "levers used in target questions.",
    },
]


# ===========================================================================
# Instance tables (schema + seed rows). Derived columns carry expressions.
# ===========================================================================

TABLES: List[Dict[str, Any]] = [
    {
        "key": "kpi",
        "label": "Company KPI (year)",
        "entity_key": "vnb_summary",
        "description": "Company-level annual VNB / APE / margin summary.",
        "columns": [
            {"key": "year", "label": "Year", "data_type": "INTEGER"},
            {"key": "month", "label": "Month", "data_type": "STRING"},
            {
                "key": "ape",
                "label": "Total APE",
                "data_type": "FLOAT",
                "unit": "$M",
                "fmt": "currency_m",
                "required": 1,
            },
            {
                "key": "vnb",
                "label": "Total VNB",
                "data_type": "FLOAT",
                "unit": "$M",
                "fmt": "currency_m",
                "required": 1,
            },
            {
                "key": "margin",
                "label": "Overall VNB Margin",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
                "required": 1,
            },
        ],
        "rows": [
            {"year": 2025, "ape": 68.00, "vnb": 11.08, "margin": 0.163},
        ],
    },
    {
        "key": "products",
        "label": "Insurance Products",
        "entity_key": "insurance_product",
        "description": "Product economics: VNB margin, commission, expense.",
        "columns": [
            {"key": "id", "label": "Product ID", "data_type": "STRING", "required": 1},
            {
                "key": "name",
                "label": "Product Name",
                "data_type": "STRING",
                "required": 1,
            },
            {
                "key": "type",
                "label": "Product Type",
                "data_type": "STRING",
                "enum_values": "NonParticipating,Participating,UnitLinked,UniversalLife",
            },
            {
                "key": "margin",
                "label": "VNB Margin",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
                "required": 1,
            },
            {
                "key": "comm",
                "label": "Commission Rate",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
            },
            {
                "key": "exp",
                "label": "Expense Ratio",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
            },
            {
                "key": "risk",
                "label": "Risk Discount Rate",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
            },
        ],
        "rows": [
            {
                "id": "Product:TermLife",
                "name": "Term Life",
                "type": "NonParticipating",
                "margin": 0.10,
                "comm": 0.15,
                "exp": 0.10,
                "risk": 0.08,
            },
            {
                "id": "Product:ParticipatingLife",
                "name": "Participating Life",
                "type": "Participating",
                "margin": 0.18,
                "comm": 0.20,
                "exp": 0.12,
            },
            {
                "id": "Product:UnitLinked",
                "name": "Unit-Linked",
                "type": "UnitLinked",
                "margin": 0.25,
                "comm": 0.25,
                "exp": 0.15,
            },
            {
                "id": "Product:UniversalLife",
                "name": "Universal Life",
                "type": "UniversalLife",
                "margin": 0.15,
                "comm": 0.22,
                "exp": 0.14,
            },
        ],
    },
    {
        "key": "mix",
        "label": "Channel-Product Mix",
        "entity_key": "channel_product_mix",
        "description": "APE share of each product within each channel.",
        "columns": [
            {
                "key": "channel_id",
                "label": "Channel ID",
                "data_type": "STRING",
                "required": 1,
            },
            {
                "key": "product_id",
                "label": "Product ID",
                "data_type": "STRING",
                "required": 1,
            },
            {
                "key": "weight",
                "label": "Mix Weight",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
                "required": 1,
            },
            {"key": "year", "label": "Year", "data_type": "INTEGER"},
            {
                "key": "pmargin",
                "label": "Product VNB Margin",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
                "expression": "lookup('products', 'id', product_id, 'margin')",
            },
            {
                "key": "pname",
                "label": "Product Name",
                "data_type": "STRING",
                "expression": "lookup('products', 'id', product_id, 'name')",
            },
            {
                "key": "cname",
                "label": "Channel Name",
                "data_type": "STRING",
                "expression": "lookup('channels', 'id', channel_id, 'name')",
            },
        ],
        "rows": [
            {
                "channel_id": "Channel:Agency",
                "product_id": "Product:TermLife",
                "weight": 0.10,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Agency",
                "product_id": "Product:ParticipatingLife",
                "weight": 0.70,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Agency",
                "product_id": "Product:UnitLinked",
                "weight": 0.15,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Agency",
                "product_id": "Product:UniversalLife",
                "weight": 0.05,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Banca",
                "product_id": "Product:TermLife",
                "weight": 0.05,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Banca",
                "product_id": "Product:ParticipatingLife",
                "weight": 0.20,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Banca",
                "product_id": "Product:UnitLinked",
                "weight": 0.65,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Banca",
                "product_id": "Product:UniversalLife",
                "weight": 0.10,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Brokerage",
                "product_id": "Product:TermLife",
                "weight": 0.05,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Brokerage",
                "product_id": "Product:ParticipatingLife",
                "weight": 0.15,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Brokerage",
                "product_id": "Product:UnitLinked",
                "weight": 0.20,
                "year": 2025,
            },
            {
                "channel_id": "Channel:Brokerage",
                "product_id": "Product:UniversalLife",
                "weight": 0.60,
                "year": 2025,
            },
        ],
    },
    {
        "key": "channels",
        "label": "Distribution Channels",
        "entity_key": "distribution_channel",
        "description": "Channels with headcount, churn and per-agent productivity.",
        "columns": [
            {"key": "id", "label": "Channel ID", "data_type": "STRING", "required": 1},
            {
                "key": "name",
                "label": "Channel Name",
                "data_type": "STRING",
                "required": 1,
            },
            {
                "key": "count",
                "label": "Agent Count",
                "data_type": "INTEGER",
                "required": 1,
            },
            {
                "key": "churn",
                "label": "Churn Rate",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
            },
            {
                "key": "prod",
                "label": "Avg Productivity",
                "data_type": "FLOAT",
                "unit": "$K",
                "fmt": "currency_k",
                "decimals": 0,
            },
            {
                "key": "prod_label",
                "label": "Productivity Label",
                "data_type": "STRING",
                "expression": "money_k(prod)",
            },
            {
                "key": "ape",
                "label": "Channel APE",
                "data_type": "FLOAT",
                "unit": "$M",
                "fmt": "currency_m",
            },
            {
                "key": "blended",
                "label": "Blended VNB Margin",
                "data_type": "FLOAT",
                "unit": "%",
                "fmt": "percent",
                "decimals": 4,
            },
            {
                "key": "vnb",
                "label": "Channel VNB",
                "data_type": "FLOAT",
                "unit": "$M",
                "fmt": "currency_m",
                "decimals": 3,
            },
        ],
        "rows": [
            {
                "id": "Channel:Agency",
                "name": "Agency",
                "count": 500,
                "churn": 0.30,
                "prod": 40,
            },
            {
                "id": "Channel:Banca",
                "name": "Banca",
                "count": 200,
                "churn": 0.10,
                "prod": 150,
            },
            {
                "id": "Channel:Brokerage",
                "name": "Brokerage",
                "count": 80,
                "churn": 0.15,
                "prod": 225,
            },
        ],
    },
    {
        "key": "agents",
        "label": "Agents",
        "entity_key": "agent",
        "description": "Individual agents: tenure, productivity, certification, churn risk.",
        "columns": [
            {"key": "id", "label": "Agent ID", "data_type": "STRING", "required": 1},
            {
                "key": "channel_id",
                "label": "Channel ID",
                "data_type": "STRING",
                "required": 1,
            },
            {
                "key": "tenure",
                "label": "Tenure (months)",
                "data_type": "INTEGER",
                "unit": "months",
            },
            {
                "key": "productivity",
                "label": "Productivity",
                "data_type": "FLOAT",
                "unit": "$K",
                "fmt": "currency_k",
                "decimals": 0,
            },
            {
                "key": "certification",
                "label": "Certification",
                "data_type": "STRING",
                "enum_values": "Term Only,Participating,Unit-Linked,Universal Life,All Products",
            },
            {
                "key": "churn_risk",
                "label": "Churn Risk",
                "data_type": "STRING",
                "enum_values": "Low,Medium,High",
            },
            {
                "key": "status",
                "label": "Status (latest month)",
                "data_type": "STRING",
                "enum_values": "Active,Inactive",
            },
            {
                "key": "channel_name",
                "label": "Channel Name",
                "data_type": "STRING",
                "expression": "lookup('channels', 'id', channel_id, 'name')",
            },
        ],
        "rows": [
            {
                "id": "Agent:A001",
                "channel_id": "Channel:Agency",
                "tenure": 24,
                "productivity": 60,
                "certification": "Participating",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A002",
                "channel_id": "Channel:Agency",
                "tenure": 6,
                "productivity": 30,
                "certification": "Term Only",
                "churn_risk": "High",
            },
            {
                "id": "Agent:A003",
                "channel_id": "Channel:Agency",
                "tenure": 36,
                "productivity": 75,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A004",
                "channel_id": "Channel:Agency",
                "tenure": 12,
                "productivity": 45,
                "certification": "Participating",
                "churn_risk": "Medium",
            },
            {
                "id": "Agent:A005",
                "channel_id": "Channel:Agency",
                "tenure": 48,
                "productivity": 90,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A006",
                "channel_id": "Channel:Agency",
                "tenure": 18,
                "productivity": 55,
                "certification": "Participating",
                "churn_risk": "Medium",
            },
            {
                "id": "Agent:A007",
                "channel_id": "Channel:Agency",
                "tenure": 30,
                "productivity": 70,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A008",
                "channel_id": "Channel:Agency",
                "tenure": 9,
                "productivity": 40,
                "certification": "Term Only",
                "churn_risk": "High",
            },
            {
                "id": "Agent:A009",
                "channel_id": "Channel:Banca",
                "tenure": 36,
                "productivity": 120,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A010",
                "channel_id": "Channel:Banca",
                "tenure": 12,
                "productivity": 90,
                "certification": "Unit-Linked",
                "churn_risk": "Medium",
            },
            {
                "id": "Agent:A011",
                "channel_id": "Channel:Banca",
                "tenure": 24,
                "productivity": 110,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A012",
                "channel_id": "Channel:Banca",
                "tenure": 6,
                "productivity": 70,
                "certification": "Unit-Linked",
                "churn_risk": "High",
            },
            {
                "id": "Agent:A013",
                "channel_id": "Channel:Banca",
                "tenure": 48,
                "productivity": 130,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A014",
                "channel_id": "Channel:Banca",
                "tenure": 30,
                "productivity": 105,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A015",
                "channel_id": "Channel:Brokerage",
                "tenure": 48,
                "productivity": 160,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A016",
                "channel_id": "Channel:Brokerage",
                "tenure": 24,
                "productivity": 140,
                "certification": "Universal Life",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A017",
                "channel_id": "Channel:Brokerage",
                "tenure": 36,
                "productivity": 150,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A018",
                "channel_id": "Channel:Brokerage",
                "tenure": 12,
                "productivity": 120,
                "certification": "Universal Life",
                "churn_risk": "Medium",
            },
            {
                "id": "Agent:A019",
                "channel_id": "Channel:Brokerage",
                "tenure": 60,
                "productivity": 180,
                "certification": "All Products",
                "churn_risk": "Low",
            },
            {
                "id": "Agent:A020",
                "channel_id": "Channel:Brokerage",
                "tenure": 18,
                "productivity": 130,
                "certification": "Universal Life",
                "churn_risk": "Medium",
            },
        ],
    },
]

METRICS: List[Dict[str, Any]] = [
    {
        "key": "year",
        "label": "Business Year",
        "expression": "cell('kpi', 'year')",
        "unit": "",
        "fmt": "number",
        "decimals": 0,
    },
    {
        "key": "stated_ape",
        "label": "Stated Total APE",
        "expression": "cell('kpi', 'ape')",
        "unit": "$M",
        "fmt": "currency_m",
    },
    {
        "key": "stated_vnb",
        "label": "Stated Total VNB",
        "expression": "cell('kpi', 'vnb')",
        "unit": "$M",
        "fmt": "currency_m",
    },
    {
        "key": "stated_margin",
        "label": "Stated VNB Margin",
        "expression": "cell('kpi', 'margin')",
        "unit": "%",
        "fmt": "percent",
        "decimals": 4,
    },
    {
        "key": "derived_ape",
        "label": "Derived Total APE",
        "expression": "round(sum(rows('channels'), 'ape'), 2)",
        "unit": "$M",
        "fmt": "currency_m",
    },
    {
        "key": "derived_vnb",
        "label": "Derived Total VNB",
        "expression": "round(sum(rows('channels'), 'vnb'), 3)",
        "unit": "$M",
        "fmt": "currency_m",
        "decimals": 3,
    },
    {
        "key": "derived_margin",
        "label": "Derived VNB Margin",
        "expression": "round(derived_vnb / derived_ape, 4)",
        "unit": "%",
        "fmt": "percent",
        "decimals": 4,
    },
    {
        "key": "gap_vnb",
        "label": "VNB Gap",
        "expression": "round(derived_vnb - stated_vnb, 2)",
        "unit": "$M",
        "fmt": "currency_m",
    },
    {
        "key": "gap_margin",
        "label": "Margin Gap",
        "expression": "round(derived_margin - stated_margin, 4)",
        "unit": "%",
        "fmt": "percent",
        "decimals": 4,
    },
]

NARRATIVE = """## How the ontology explains the business

The knowledge graph connects a top-level financial KPI (**Total VNB**) to operational drivers (agents, productivity, churn) and product economics (mix weights, product margins) through **{{ links | length }} directed edges of {{ edge_types | length }} types**.

### 1. Structural identity chain (STRUCTURAL)
- TotalVNB = Σ_channel (ChannelAPE × ChannelBlendedMargin)
- ChannelBlendedMargin = Σ_product (product_mix_weight × product_vnb_margin)
- ChannelAPE = agent_count × avg_productivity

### 2. Channel decomposition (computed from the ontology)
| Channel | Agents × Productivity | APE | Blended margin | VNB |
|---|---|---|---|---|
{% for r in channels -%}
| {{ r.name }} | {{ r.count }} × {{ r.prod_label }} | {{ r.ape | money }} | {{ r.blended | pct }} | {{ r.vnb | money }} |
{% endfor -%}
| **Total** | — | **{{ m.derived_ape | money }}** | **{{ m.derived_margin | pct }}** | **{{ m.derived_vnb | money }}** |

### 3. Reconciliation gap explained by the graph
- Ontology-derived VNB = **{{ m.derived_vnb | money }}** vs stated **{{ m.stated_vnb | money }}** → a **{{ m.gap_vnb | money }} gap** ({{ m.derived_margin | pct }} vs {{ m.stated_margin | pct }} margin).
- Walking the graph attributes the gap to mix realization and assumption/margin change.

### 4. Cross-cutting links (why this is a graph, not a tree)
- **E9** Certification → Product-Mix Eligibility: Term Only agents cannot sell high-margin Unit-Linked.
- **E10** Churn Risk → Agent Count: high-risk agents (e.g. A002, A008, A012) feed churn, shrinking channel APE.
- **E11** Commission/Expense → Product VNB Margin: product economics influence margin directly.
"""


# ===========================================================================
# 25 deterministic question rules (patterns + Jinja answer templates)
# ===========================================================================


def _r(
    sort: int,
    key: str,
    category: str,
    question: str,
    template: str,
    nodes: List[str],
    path: List[str],
    patterns: List[str] = None,
    require_groups: List[List[str]] = None,
    guards: List[str] = None,
) -> Dict[str, Any]:
    return {
        "sort": sort,
        "key": key,
        "category": category,
        "question": question,
        "template": template.strip() + "\n",
        "nodes": nodes,
        "path": path,
        "patterns": patterns or [],
        "require_groups": require_groups or [],
        "guards": guards or [],
    }


RULES: List[Dict[str, Any]] = [
    _r(
        1,
        "total_vnb",
        "Financial KPI",
        "What is the total VNB?",
        """
Total **VNB** for {{ kpi.year }} is **{{ m.stated_vnb | money }}**.

| Metric | Value |
|---|---|
| Total APE | {{ m.stated_ape | money }} |
| VNB Margin | {{ m.stated_margin | pct }} |
| VNB | {{ m.stated_vnb | money }} |
""",
        ["TotalVNB", "TotalAPE", "BlendedVNBMargin"],
        ["TotalAPE", "BlendedVNBMargin", "TotalVNB"],
        patterns=[
            r"total vnb",
            r"overall vnb",
            r"what is vnb",
            r"total value of new business",
            r"总vnb",
            r"整体vnb",
            r"vnb是多少",
            r"新业务价值",
        ],
    ),
    _r(
        2,
        "total_ape",
        "Financial KPI",
        "What is the total APE?",
        """
Total **APE** is **{{ m.stated_ape | money }}**, derived as Σ(channel agent_count × productivity) = {{ m.derived_ape | money }}.
""",
        ["TotalAPE", "ChannelAPE"],
        ["ChannelAPE", "TotalAPE"],
        patterns=[
            r"total ape",
            r"overall ape",
            r"what is ape",
            r"总ape",
            r"整体ape",
            r"ape是多少",
            r"保费当量",
        ],
    ),
    _r(
        3,
        "overall_margin",
        "Financial KPI",
        "What is the overall margin?",
        """
Overall **VNB margin** is **{{ m.stated_margin | pct }}**. However the **mix-implied margin is {{ m.derived_margin | pct }}** → a {{ m.gap_vnb | money }} gap.
""",
        ["BlendedVNBMargin", "TotalVNB"],
        ["ChannelBlendedMargin", "TotalVNB"],
        patterns=[
            r"margin",
            r"profitability of the whole",
            r"overall margin",
            r"边际",
            r"利润率",
            r"margin率",
        ],
        guards=[r"product", r"channel", r"which", r"产品", r"渠道"],
    ),
    _r(
        4,
        "vnb_gap",
        "Ontology Reconciliation",
        "What is the VNB gap?",
        """
The ontology-derived VNB is **{{ m.derived_vnb | money }}** vs the stated **{{ m.stated_vnb | money }}** → a **{{ m.gap_vnb | money }} gap** ({{ m.derived_margin | pct }} vs {{ m.stated_margin | pct }} margin). This is attributable to **mix realization / assumption change**.
""",
        ["TotalVNB", "TotalAPE", "BlendedVNBMargin"],
        ["ChannelBlendedMargin", "DerivedVNB", "StatedVNB", "Gap"],
        patterns=[
            r"gap",
            r"reconcil",
            r"derived",
            r"difference",
            r"why.*vnb",
            r"unexplained",
            r"差异",
            r"缺口",
            r"勾稽",
            r"调节",
            r"为什么",
        ],
    ),
    _r(
        5,
        "product_highest",
        "Product",
        "Which product has the highest margin?",
        """
{% set best = max_by(products, 'margin') -%}
The highest-margin product is **{{ best.name }}** at **{{ best.margin | pct }}** (commission {{ best.comm | pct }}).

| Product | Margin | Commission | Expense |
|---|---|---|---|
{% for p in products -%}
| {{ p.name }} | {{ p.margin | pct }} | {{ p.comm | pct }} | {{ p.exp | pct }} |
{% endfor %}
""",
        ["ProductVNBMargin", "ChannelBlendedMargin"],
        ["ProductVNBMargin", "ChannelBlendedMargin"],
        patterns=[
            r"highest.*product",
            r"product.*highest",
            r"most profitable product",
            r"best margin product",
            r"which product.*margin",
            r"最高.*产品",
            r"产品.*最高",
            r"边际最高",
        ],
    ),
    _r(
        6,
        "product_lowest",
        "Product",
        "Which product has the lowest margin?",
        """
{% set worst = min_by(products, 'margin') -%}
The lowest-margin product is **{{ worst.name }}** at **{{ worst.margin | pct }}**.
""",
        ["ProductVNBMargin", "ChannelBlendedMargin"],
        ["ProductVNBMargin", "ChannelBlendedMargin"],
        patterns=[
            r"lowest.*product",
            r"product.*lowest",
            r"worst margin",
            r"最低.*产品",
            r"产品.*最低",
        ],
    ),
    _r(
        7,
        "product_commission",
        "Product",
        "Which product has the highest commission?",
        """
{% set top = max_by(products, 'comm') -%}
Highest commission is **{{ top.name }}** at **{{ top.comm | pct }}**. Higher-margin products tend to carry higher commission.
""",
        ["CommissionExpense"],
        ["CommissionExpense", "ProductVNBMargin"],
        require_groups=[[r"commission", r"佣金"], [r"product", r"产品"]],
    ),
    _r(
        8,
        "product_types",
        "Product",
        "What product types exist?",
        """
Product types: **{% for p in products %}{{ p.name }}{% if not loop.last %}, {% endif %}{% endfor %}**.
""",
        ["ProductVNBMargin"],
        ["ProductVNBMargin"],
        patterns=[
            r"product type",
            r"types of product",
            r"what products",
            r"产品类型",
            r"有哪些产品",
            r"什么产品",
        ],
    ),
    _r(
        9,
        "channel_highest_vnb",
        "Distribution Channel",
        "Which channel has the highest VNB?",
        """
{% set top = max_by(channels, 'vnb') -%}
The channel with the highest VNB is **{{ top.name }}** at **{{ top.vnb | money }}** (blended margin {{ top.blended | pct }}, APE {{ top.ape | money }}).
""",
        ["Channel:Agency", "Channel:Banca", "Channel:Brokerage", "TotalVNB"],
        ["ChannelAPE", "ChannelBlendedMargin", "ChannelVNB"],
        patterns=[
            r"(channel|which.*channel).*highest.*vnb",
            r"highest vnb.*channel",
            r"vnb.*channel",
            r"渠道.*vnb.*最高",
            r"vnb最高.*渠道",
        ],
    ),
    _r(
        10,
        "channel_most_agents",
        "Distribution Channel",
        "Which channel has the most agents?",
        """
{% set top = max_by(channels, 'count') -%}
The channel with the most agents is **{{ top.name }}** with **{{ top.count }}** agents.
""",
        ["Channel:Agency", "Channel:Banca", "Channel:Brokerage"],
        ["AgentCount", "ChannelAPE"],
        patterns=[
            r"(channel|which.*channel).*most agents",
            r"most agents.*channel",
            r"渠道.*代理人.*最多",
            r"最多代理人",
        ],
    ),
    _r(
        11,
        "channel_highest_churn",
        "Distribution Channel",
        "Which channel has the highest churn?",
        """
{% set top = max_by(channels, 'churn') -%}
The channel with the highest churn is **{{ top.name }}** at **{{ top.churn | pct }}**.
""",
        ["Channel:Agency", "Channel:Banca", "Channel:Brokerage", "AgentCount"],
        ["AgentChurnRate", "AgentCount"],
        patterns=[
            r"(channel|which.*channel).*highest churn",
            r"churn.*channel",
            r"highest churn",
            r"流失率最高",
            r"流失.*渠道",
        ],
    ),
    _r(
        12,
        "channel_highest_ape",
        "Distribution Channel",
        "Which channel has the highest APE?",
        """
{% set top = max_by(channels, 'ape') -%}
The channel generating the most APE is **{{ top.name }}** at **{{ top.ape | money }}** ({{ top.count }} agents × {{ top.prod_label }}).
""",
        ["Channel:Agency", "Channel:Banca", "Channel:Brokerage", "TotalAPE"],
        ["AgentCount", "AgentProductivity", "ChannelAPE"],
        patterns=[
            r"(channel|which.*channel).*highest.*ape",
            r"ape.*channel",
            r"highest ape",
            r"渠道.*ape.*最高",
            r"ape最高",
        ],
    ),
    _r(
        13,
        "channel_highest_margin",
        "Distribution Channel",
        "Which channel has the highest margin?",
        """
{% set top = max_by(channels, 'blended') -%}
The channel with the highest blended margin is **{{ top.name }}** at **{{ top.blended | pct }}**, driven by its product mix.
""",
        ["Channel:Agency", "Channel:Banca", "Channel:Brokerage", "BlendedVNBMargin"],
        ["ProductMixWeight", "ChannelBlendedMargin"],
        patterns=[
            r"(channel|which.*channel).*highest.*margin",
            r"margin.*channel",
            r"blended margin",
            r"渠道.*边际.*最高",
            r"边际最高.*渠道",
        ],
    ),
    _r(
        14,
        "channel_productive",
        "Distribution Channel",
        "Which channel is most productive?",
        """
{% set top = max_by(channels, 'prod') -%}
The most productive channel per agent is **{{ top.name }}** at **{{ top.prod_label }}**.
""",
        ["Channel:Agency", "Channel:Banca", "Channel:Brokerage"],
        ["AgentProductivity", "ChannelAPE"],
        patterns=[
            r"most productive.*channel",
            r"productive.*channel",
            r"channel.*productive",
            r"产能最高.*渠道",
            r"渠道.*产能",
        ],
    ),
    _r(
        15,
        "channel_count",
        "Distribution Channel",
        "How many channels?",
        """
There are **{{ channels | length }}** channels: **{% for c in channels %}{{ c.name }}{% if not loop.last %}, {% endif %}{% endfor %}**.

| Channel | Agents | Churn | Avg Prod | Blended | VNB |
|---|---|---|---|---|---|
{% for r in channels -%}
| {{ r.name }} | {{ r.count }} | {{ r.churn | pct }} | {{ r.prod_label }} | {{ r.blended | pct }} | {{ r.vnb | money }} |
{% endfor %}
""",
        ["Channel:Agency", "Channel:Banca", "Channel:Brokerage"],
        ["ChannelAPE", "ChannelBlendedMargin", "TotalVNB"],
        patterns=[
            r"how many channel",
            r"channel.*many",
            r"list.*channel",
            r"多少.*渠道",
            r"渠道.*数量",
            r"几个渠道",
            r"哪些渠道",
        ],
    ),
    _r(
        16,
        "agent_high_churn",
        "Agent",
        "Which agents are at high churn risk?",
        """
{%- set high = where(agents, 'churn_risk', 'High') -%}
Agents at **High churn risk**: **{% for a in high %}{{ a.id }} ({{ a.channel_name }}){% if not loop.last %}, {% endif %}{% endfor %}**.
""",
        ["AgentCount", "ChurnRisk"],
        ["ChurnRisk", "AgentCount"],
        patterns=[
            r"high.*churn",
            r"churn.*risk",
            r"at risk",
            r"churn risk",
            r"which agents.*risk",
            r"高流失",
            r"流失风险",
            r"哪些代理人.*风险",
        ],
    ),
    _r(
        17,
        "agent_most_productive",
        "Agent",
        "Which agent is most productive?",
        """
{% set top = max_by(agents, 'productivity') -%}
The most productive agent is **{{ top.id }}** ({{ top.channel_name }}) at **${{ top.productivity | int }}K**, tenure {{ top.tenure }} months.
""",
        ["AgentProductivity"],
        ["AgentProductivity", "ChannelAPE"],
        patterns=[
            r"most productive",
            r"top agent",
            r"best agent",
            r"highest productivity",
            r"产能最高.*代理人",
            r"代理人.*产能最高",
            r"最优秀代理人",
        ],
    ),
    _r(
        18,
        "agent_tenure",
        "Agent",
        "What is the average agent tenure?",
        """
Average agent tenure per channel:

{% for c in channels -%}
{% set ags = where(agents, 'channel_id', c.id) -%}
- **{{ c.name }}**: {{ avg(ags, 'tenure') | round(0) | int }} months ({{ ags | length }} agents)
{% endfor %}
""",
        ["AgentCount"],
        ["AgentCount", "ChannelAPE"],
        patterns=[
            r"average.*tenure",
            r"avg.*tenure",
            r"tenure.*channel",
            r"平均司龄",
            r"司龄",
        ],
    ),
    _r(
        19,
        "agent_certified",
        "Agent",
        "Which agents are certified for all products?",
        """
{%- set ac = where(agents, 'certification', 'All Products') -%}
**{{ ac | length }}** agents are certified for **All Products**: {{ ac | map(attribute='id') | join(', ') }}.
""",
        ["Certification", "ProductMixEligibility"],
        ["Certification", "ProductMixEligibility"],
        patterns=[
            r"certif",
            r"all products",
            r"unit-linked.*agent",
            r"认证",
            r"全产品",
            r"资质",
        ],
    ),
    _r(
        20,
        "product_mix",
        "Product Mix",
        "What is the product mix?",
        """
Product mix per channel:

{% for c in channels -%}
- **{{ c.name }}**: {% for p in products %}{{ p.name }}: {{ mw(c.id, p.id) | pct }}{% if not loop.last %}, {% endif %}{% endfor %}
{% endfor %}
""",
        ["ProductMixWeight", "ChannelBlendedMargin"],
        ["ProductMixWeight", "ChannelBlendedMargin"],
        patterns=[
            r"product mix",
            r"mix.*channel",
            r"what.*mix",
            r"sales composition",
            r"产品组合",
            r"组合占比",
            r"业务组合",
        ],
    ),
    _r(
        21,
        "product_dominant",
        "Product Mix",
        "What is the dominant product per channel?",
        """
Dominant product per channel:

{% for c in channels -%}
{% set d = max_by(where(mix, 'channel_id', c.id), 'weight') -%}
- **{{ c.name }}**: {{ d.pname }} ({{ d.weight | pct }})
{% endfor %}
""",
        ["ProductMixWeight", "ChannelBlendedMargin"],
        ["ProductMixWeight", "ChannelBlendedMargin"],
        patterns=[
            r"dominate",
            r"dominant product",
            r"most sold",
            r"biggest.*product.*channel",
            r"主导产品",
            r"主要产品",
            r"占比最高.*产品",
        ],
    ),
    _r(
        22,
        "cert_gate",
        "Ontology Cross-Link",
        "Does certification gate product eligibility?",
        """
Yes — **certification gates product eligibility**. Agents certified "Term Only" (e.g. A002, A008) cannot sell higher-margin products like Unit-Linked. This is a **cross-link**: certification → mix eligibility → blended margin → VNB.
""",
        ["Certification", "ProductMixEligibility", "ChannelBlendedMargin", "TotalVNB"],
        ["Certification", "ProductMixEligibility", "ChannelBlendedMargin", "TotalVNB"],
        patterns=[
            r"certif.*gate",
            r"eligib",
            r"product.*sell",
            r"which.*can sell",
            r"gate",
            r"资质.*限制",
            r"认证.*门槛",
            r"可销售",
            r"销售资格",
        ],
    ),
    _r(
        23,
        "churn_feed",
        "Ontology Cross-Link",
        "How does churn affect VNB?",
        """
Churn **feeds forward into VNB**: high churn ({{ channels[0].name }} {{ channels[0].churn | pct0 }}) reduces agent count → lowers channel APE → lowers VNB. Path: **AgentChurnRate → AgentCount → ChannelAPE → TotalVNB**.
""",
        ["AgentChurnRate", "AgentCount", "ChannelAPE", "TotalVNB"],
        ["AgentChurnRate", "AgentCount", "ChannelAPE", "TotalVNB"],
        patterns=[
            r"churn.*feed",
            r"churn.*vnb",
            r"churn.*impact",
            r"churn.*affect",
            r"流失.*影响",
            r"流失.*vnb",
            r"流失如何",
        ],
    ),
    _r(
        25,
        "ontology_layer",
        "Ontology Layer",
        "What is the ontology layer?",
        """
The ontology is a **graph, not a strict tree** — it links financial KPIs (VNB) to operational drivers (channels, agents, mix) via {{ edge_defs | length }} edges across 4 types (STRUCTURAL, OPERATIONAL, MIX, CROSS_LINK).
""",
        ["TotalVNB", "TotalAPE", "BlendedVNBMargin"],
        ["TotalAPE", "ChannelBlendedMargin", "TotalVNB"],
        patterns=[
            r"ontology",
            r"graph",
            r"relationship",
            r"how.*linked",
            r"layer",
            r"本体",
            r"图谱",
            r"关系",
            r"节点",
        ],
    ),
    _r(
        26,
        "monthly_trend",
        "Trend",
        "How has VNB trended month by month?",
        """
{% if ts %}## VNB monthly trend ({{ ts[0].label }} – {{ ts[-1].label }})
| Month | Total APE ($M) | Total VNB ($M) | MoM VNB | Agency VNB ($M) | Agency share |
| --- | ---: | ---: | ---: | ---: | ---: |
{% for r in ts %}| {{ r.label }} | {{ '%.2f'|format(r.total_ape) }} | {{ '%.2f'|format(r.total_vnb) }} | {{ '%+.1f%%'|format(r.mom_vnb * 100) }} | {{ '%.2f'|format(r.agency_vnb) }} | {{ '%.0f%%'|format(r.agency_share * 100) }} |
{% endfor %}
Latest: {{ ts[-1].narrative }}
Total VNB moved from ${{ '%.2f'|format(ts[0].total_vnb) }}M to ${{ '%.2f'|format(ts[-1].total_vnb) }}M over {{ ts|length }} months.{% else %}Monthly trend data is not available in the loaded pack.{% endif %}
""",
        ["TotalVNB", "TotalAPE", "AgencyVNB"],
        ["VNB Monthly"],
        patterns=[
            r"trend",
            r"over time",
            r"month by month",
            r"monthly",
            r"by month",
            r"each month",
            r"history",
            r"走势",
            r"趋势",
            r"每月",
            r"各月",
            r"月度",
        ],
    ),
    _r(
        27,
        "mom_attribution",
        "Driver attribution",
        "What drove the latest VNB change month over month?",
        """
{% if attr %}## {{ attr.label }} VNB change vs {{ attr.prev_label }}
Total VNB moved ${{ '%.2f'|format(attr.prev_total_vnb) }}M → ${{ '%.2f'|format(attr.cur_total_vnb) }}M ({{ '%+.1f%%'|format(attr.mom_pct * 100) }}, {{ '%+.3f'|format(attr.delta_total_vnb) }} $M).

Using the identity **VNB = Active agents × Avg productivity × Weighted margin**, the change splits with no residual:

| Driver | Effect on VNB ($M) |
| --- | ---: |
| Active-agent effect | {{ '%+.3f'|format(attr.factors.agent_effect) }} |
| Productivity effect | {{ '%+.3f'|format(attr.factors.productivity_effect) }} |
| Mix / margin effect | {{ '%+.3f'|format(attr.factors.mix_effect) }} |
| **Total** | **{{ '%+.3f'|format(attr.factors.delta_vnb) }}** |

Per channel:
{% for c in attr.channels %}- **{{ c.channel_name }}** {{ '%+.3f'|format(c.delta_vnb) }} $M = agents {{ '%+.3f'|format(c.agent_effect) }} + productivity {{ '%+.3f'|format(c.productivity_effect) }} + mix/margin {{ '%+.3f'|format(c.mix_effect) }}
{% endfor %}
{{ attr.narrative }}{% else %}Month-on-month attribution needs at least two months of data.{% endif %}
""",
        ["ActiveAgentCount", "AgentProductivity", "WeightedVNBMargin", "TotalVNB"],
        ["Channel Monthly", "VNB Monthly"],
        patterns=[
            r"drove",
            r"driver",
            r"attribution",
            r"month[- ]on[- ]month",
            r"\bmom\b",
            r"why did vnb",
            r"vnb change",
            r"change in vnb",
            r"环比",
            r"归因",
            r"变动",
            r"驱动",
            r"原因",
        ],
    ),
    _r(
        28,
        "scenario_target",
        "Scenario back-solve",
        "What would it take to double next month's VNB?",
        """
{% if scen %}## What it takes to hit {{ '%.0fx'|format(scen.multiplier) }} VNB (base: {{ scen.label }})
- Total VNB: ${{ '%.2f'|format(scen.current_total_vnb) }}M → target **${{ '%.2f'|format(scen.target_total_vnb) }}M** (gap +${{ '%.2f'|format(scen.gap_total_vnb) }}M).
- Holding non-Agency channels flat at ${{ '%.2f'|format(scen.non_agency_vnb_held) }}M, **Agency VNB must rise** ${{ '%.2f'|format(scen.current_agency_vnb) }}M → **${{ '%.2f'|format(scen.required_agency_vnb) }}M** ({{ '%+.0f%%'|format(scen.agency_vnb_uplift * 100) }}).
- At the current {{ '%.1f%%'|format(scen.agency_margin * 100) }} weighted margin that means Agency APE ${{ '%.2f'|format(scen.current_agency_ape) }}M → **${{ '%.2f'|format(scen.required_agency_ape) }}M**.

Two pure levers (product mix / margin held constant):
1. **Headcount lever** — at current productivity ${{ '%.2f'|format(scen.lever_headcount.current_prod) }}k, active agents {{ scen.lever_headcount.current_active }} → **{{ '%.0f'|format(scen.lever_headcount.required_active) }}** ({{ '%+.0f%%'|format(scen.lever_headcount.active_uplift * 100) }}).
2. **Productivity lever** — keeping {{ scen.lever_productivity.planned_active }} active agents, avg productivity ${{ '%.2f'|format(scen.lever_productivity.current_prod) }}k → **${{ '%.2f'|format(scen.lever_productivity.required_prod) }}k** ({{ '%+.0f%%'|format(scen.lever_productivity.prod_uplift * 100) }}).

A third lever is shifting product mix to lift the weighted VNB margin. Adjust the target or planned headcount in the Scenario panel for a custom plan.{% else %}Scenario data is not available in the loaded pack.{% endif %}
""",
        [
            "TargetTotalVNB",
            "OperationalLevers",
            "ActiveAgentCount",
            "AgentProductivity",
        ],
        ["Scenario Analysis", "Channel Monthly"],
        patterns=[
            r"double",
            r"\b2x\b",
            r"target",
            r"scenario",
            r"back[- ]?solve",
            r"what would it take",
            r"required to",
            r"achieve",
            r"reach.*vnb",
            r"翻倍",
            r"目标",
            r"情景",
            r"达标",
            r"需要多少",
            r"达成",
        ],
    ),
    _r(
        29,
        "agent_health",
        "Agent analytics",
        "What is the health of the agent salesforce?",
        """
{% if astats %}## Salesforce health — {{ astats.label }}
- **{{ astats.total_active }} / {{ astats.total_registered }}** registered agents are active ({{ '%.0f%%'|format(astats.active_ratio * 100) }} active rate).
- Churn risk among active agents: {% for k, v in astats.churn_risk.items() %}{{ k }} {{ v }}{% if not loop.last %}, {% endif %}{% endfor %} — the High-risk {{ astats.churn_risk.get('High', 0) }} are the retention focus.

| Channel | Registered | Active | Active % | Avg productivity ($k) |
| --- | ---: | ---: | ---: | ---: |
{% for c in astats.by_channel %}| {{ c.channel_id }} | {{ c.registered }} | {{ c.active }} | {{ '%.0f%%'|format(c.active_ratio * 100) }} | {{ '%.2f'|format(c.avg_prod) }} |
{% endfor %}
Tenure of active agents (retention/experience mix): {% for k, v in astats.tenure_buckets.items() %}{{ k }} {{ v }}{% if not loop.last %} · {% endif %}{% endfor %}.{% else %}Agent-level analytics are not available in the loaded pack.{% endif %}
""",
        ["AgentStatus", "ActiveAgentCount", "AgentChurnRate", "AgentProductivity"],
        ["Agent Monthly", "Agent Master"],
        patterns=[
            r"salesforce",
            r"agent health",
            r"active agents",
            r"active rate",
            r"churn risk",
            r"certification",
            r"tenure",
            r"inactive",
            r"retention",
            r"代理人",
            r"活跃率",
            r"流失",
            r"司龄",
            r"资质",
            r"销售队伍",
            r"留存",
        ],
    ),
    _r(
        30,
        "agency_identity",
        "Causal identity",
        "How is Agency VNB built from agents and productivity?",
        """
{% set ag = channels_ts | selectattr('month', 'equalto', latest.month) | selectattr('channel_id', 'equalto', 'CH001') | list %}
{% if latest and ag %}{% set a = ag[0] %}## Agency causal identity — {{ a.label }}
- **Active agents × avg productivity = Agency APE**: {{ a.active|int }} × ${{ '%.2f'|format(a.avg_prod) }}k = **${{ '%.2f'|format(a.ape) }}M**.
- **Agency APE × weighted VNB margin = Agency VNB**: ${{ '%.2f'|format(a.ape) }}M × {{ '%.1f%%'|format(a.margin * 100) }} = **${{ '%.2f'|format(a.vnb) }}M**.
- Agency contributes **{{ '%.0f%%'|format(latest.agency_share * 100) }}** of total VNB ${{ '%.2f'|format(latest.total_vnb) }}M.

This is the explicit chain in KPI Linkage: Agent Status → Active Agent Count → Agency APE → Agency VNB → Total VNB, with Product Mix setting the weighted margin.{% else %}Agency monthly data is not available in the loaded pack.{% endif %}
""",
        [
            "AgentStatus",
            "ActiveAgentCount",
            "AgencyAPE",
            "WeightedVNBMargin",
            "AgencyVNB",
            "TotalVNB",
        ],
        ["KPI Linkage", "Channel Monthly"],
        patterns=[
            r"agency ape",
            r"agency vnb",
            r"how.*agency",
            r"agency identity",
            r"causal chain",
            r"经代",
            r"代理人渠道",
            r"因果链",
        ],
    ),
]


# ===========================================================================
# Materialization
# ===========================================================================


def _load_formal_schema() -> Dict[str, Any]:
    """Entities / attributes / relations from the BP2 formal schema file."""
    path = BASE_DIR / "data" / "ontology_BP2.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"entities": [], "attributes": [], "relations": []}


def _iso(v: Any) -> Any:
    """Normalise openpyxl values: dates -> ISO string, None stays None."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.date().isoformat() if hasattr(v, "date") else str(v)
        except Exception:  # noqa: BLE001
            return str(v)
    return v


def _make_sheet_rows(wb: Any):
    def sheet_rows(name, mapping):
        ws = wb[name] if name in wb.sheetnames else None
        if ws is None:
            return []
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h).strip() for h in rows[0]]
        data = []
        for row in rows[1:]:
            if row is None or all(c is None for c in row):
                continue
            raw = dict(zip(headers, row))
            data.append({k: (_iso(raw.get(src)) if raw.get(src) is not None else "") for k, src in mapping.items()})
        return data

    return sheet_rows


# Raw monthly tables: (key, label, entity, description, column specs, source mapping)
_RAW_TABLES = [
    (
        "vnb_monthly",
        "VNB Monthly (company)",
        "vnb_monthly",
        "Company-level monthly financial KPI summary with Agency contribution.",
        [
            ("month", "Month", "STRING", "", None),
            ("total_ape", "Total APE", "FLOAT", "$M", "currency_m"),
            ("total_vnb", "Total VNB", "FLOAT", "$M", "currency_m"),
            ("total_margin", "VNB Margin", "FLOAT", "%", "percent"),
            ("agency_ape", "Agency APE", "FLOAT", "$M", "currency_m"),
            ("agency_vnb", "Agency VNB", "FLOAT", "$M", "currency_m"),
            ("agency_share", "Agency share of VNB", "FLOAT", "%", "percent"),
            ("mom_vnb", "MoM VNB change", "FLOAT", "%", "percent"),
            ("agency_mom", "Agency VNB MoM change", "FLOAT", "%", "percent"),
            (
                "agency_contrib",
                "Agency contribution to VNB increase",
                "FLOAT",
                "$M",
                "currency_m",
            ),
            ("narrative", "Narrative driver", "STRING", "", None),
        ],
        {
            "month": "month",
            "total_ape": "Total APE ($M)",
            "total_vnb": "Total VNB ($M)",
            "total_margin": "VNB Margin",
            "agency_ape": "Agency APE ($M)",
            "agency_vnb": "Agency VNB ($M)",
            "agency_share": "Agency share of VNB",
            "mom_vnb": "MoM VNB change",
            "agency_mom": "Agency VNB MoM change",
            "agency_contrib": "Agency contribution to total VNB increase ($M)",
            "narrative": "Narrative driver",
        },
    ),
    (
        "channel_monthly",
        "Channel Monthly (KPIs)",
        "channel_monthly",
        "Monthly operational and financial KPIs by distribution channel.",
        [
            ("month", "Month", "STRING", "", None),
            ("channel_id", "Channel ID", "STRING", "", None),
            ("channel_name", "Channel", "STRING", "", None),
            ("registered", "Registered agents", "INTEGER", "", "number"),
            ("active", "Active agents", "INTEGER", "", "number"),
            ("active_ratio", "Active ratio", "FLOAT", "%", "percent"),
            ("avg_prod", "Avg productivity", "FLOAT", "$k", "currency_k"),
            ("ape", "APE", "FLOAT", "$M", "currency_m"),
            ("margin", "Weighted VNB margin", "FLOAT", "%", "percent"),
            ("vnb", "VNB", "FLOAT", "$M", "currency_m"),
            ("mom_vnb", "MoM VNB change", "FLOAT", "%", "percent"),
        ],
        {
            "month": "month",
            "channel_id": "channel_id",
            "channel_name": "channel_name",
            "registered": "registered_agents",
            "active": "active_agents",
            "active_ratio": "active_ratio",
            "avg_prod": "avg_productivity_$k",
            "ape": "APE_$M",
            "margin": "weighted_VNB_margin",
            "vnb": "VNB_$M",
            "mom_vnb": "MoM_VNB_change",
        },
    ),
    (
        "mix_monthly",
        "Product Mix Monthly",
        "product_mix_monthly",
        "Monthly Agency product mix and VNB-margin contribution.",
        [
            ("month", "Month", "STRING", "", None),
            ("channel_id", "Channel ID", "STRING", "", None),
            ("product_id", "Product ID", "STRING", "", None),
            ("product_name", "Product", "STRING", "", None),
            ("mix", "Product mix", "FLOAT", "%", "percent"),
            ("margin", "VNB margin", "FLOAT", "%", "percent"),
            ("weighted", "Weighted margin component", "FLOAT", "percent", "percent"),
        ],
        {
            "month": "month",
            "channel_id": "channel_id",
            "product_id": "product_id",
            "product_name": "product_name",
            "mix": "product_mix",
            "margin": "VNB_margin",
            "weighted": "weighted_margin_component",
        },
    ),
    (
        "agent_master",
        "Agent Master",
        "agent",
        "Static profile and attributes for each Agency agent.",
        [
            ("agent_id", "Agent ID", "STRING", "", None),
            ("channel_id", "Channel ID", "STRING", "", None),
            ("join_date", "Join date", "STRING", "", None),
            ("tenure_base", "Tenure at Jun-2025 (mo)", "INTEGER", "mo", "number"),
            ("certification", "Certification", "STRING", "", None),
            ("churn_risk", "Churn risk", "STRING", "", None),
            ("base_prod", "Base productivity index", "FLOAT", "", "number"),
        ],
        {
            "agent_id": "agent_id",
            "channel_id": "channel_id",
            "join_date": "join_date",
            "tenure_base": "tenure_months_at_Jun2025",
            "certification": "certification",
            "churn_risk": "churn_risk",
            "base_prod": "base_productivity_index",
        },
    ),
    (
        "agent_monthly",
        "Agent Monthly Performance",
        "agent_monthly",
        "Monthly operational performance for each agent (status / productivity / APE).",
        [
            ("month", "Month", "STRING", "", None),
            ("agent_id", "Agent ID", "STRING", "", None),
            ("channel_id", "Channel ID", "STRING", "", None),
            ("status", "Status", "STRING", "", None),
            ("prod", "Productivity", "FLOAT", "$k", "currency_k"),
            ("ape", "APE", "FLOAT", "$M", "currency_m"),
            ("tenure", "Tenure (mo)", "INTEGER", "mo", "number"),
            ("certification", "Certification", "STRING", "", None),
            ("churn_risk", "Churn risk", "STRING", "", None),
        ],
        {
            "month": "month",
            "agent_id": "agent_id",
            "channel_id": "channel_id",
            "status": "status",
            "prod": "productivity_$k",
            "ape": "APE_$M",
            "tenure": "tenure_months",
            "certification": "certification",
            "churn_risk": "churn_risk",
        },
    ),
    (
        "kpi_linkage",
        "KPI Linkage (ontology)",
        "",
        "Explicit causal relationships between operational drivers and financial KPIs.",
        [
            ("src", "From entity", "STRING", "", None),
            ("rel", "Relationship", "STRING", "", None),
            ("tgt", "To entity", "STRING", "", None),
            ("logic", "Calculation / logic", "STRING", "", None),
            ("interpretation", "Business interpretation", "STRING", "", None),
            ("question", "Example question enabled", "STRING", "", None),
        ],
        {
            "src": "from_entity",
            "rel": "relationship",
            "tgt": "to_entity",
            "logic": "calculation / logic",
            "interpretation": "business interpretation",
            "question": "example question enabled",
        },
    ),
    (
        "scenario_data",
        "Scenario Analysis (seed)",
        "scenario_analysis",
        "Reference back-solve from a financial VNB target to operational levers.",
        [
            ("item", "Scenario / input", "STRING", "", None),
            ("value", "Value", "STRING", "", None),
            ("unit", "Unit / interpretation", "STRING", "", None),
        ],
        {
            "item": "Scenario / input",
            "value": "Value",
            "unit": "Unit / interpretation",
        },
    ),
]


def _sheet_name(key: str) -> str:
    return {
        "vnb_monthly": "VNB Monthly",
        "channel_monthly": "Channel Monthly",
        "mix_monthly": "Product Mix Monthly",
        "agent_master": "Agent Master",
        "agent_monthly": "Agent Monthly",
        "kpi_linkage": "KPI Linkage",
        "scenario_data": "Scenario Analysis",
    }[key]


def _load_monthly_pack(wb: Any):
    """Return (compat_rows, raw_table_defs) from a monthly workbook.

    A monthly workbook carries VNB Monthly + Agent Monthly (and usually
    Channel / Product-Mix / Agent Master / KPI Linkage / Scenario sheets);
    this loader builds the canonical compat views plus the raw monthly tables
    used by the analytics layer.
    """
    from . import analytics

    sheet_rows = _make_sheet_rows(wb)

    raw = {}
    raw_defs = []
    for key, label, entity, desc, cols, mapping in _RAW_TABLES:
        rows = sheet_rows(_sheet_name(key), mapping)
        raw[key] = rows
        raw_defs.append(
            {
                "key": key,
                "label": label,
                "entity_key": entity,
                "description": desc,
                "columns": [
                    {
                        "key": ck,
                        "label": clabel,
                        "data_type": ctype,
                        "unit": cunit,
                        "fmt": cfmt or "raw",
                        "decimals": (4 if cfmt == "percent" else 3 if cfmt in ("currency_m", "currency_k") else 0),
                    }
                    for ck, clabel, ctype, cunit, cfmt in cols
                ],
                "rows": rows,
            }
        )

    months = analytics.sorted_months(raw["vnb_monthly"])
    latest = months[-1]

    # products (reference economics)
    out: Dict[str, Any] = {}
    prods = sheet_rows(
        "Product",
        {
            "id": "product_id",
            "name": "product_name",
            "type": "product_type",
            "margin": "VNB_margin",
            "comm": "commission_rate",
            "exp": "expense_ratio",
            "risk": "risk_discount_rate",
        },
    )
    for r in prods:
        r["id"] = "Product:" + str(r["id"])
    out["products"] = prods

    # kpi = latest company month
    latest_v = next(
        (r for r in raw["vnb_monthly"] if analytics._month_key(r["month"]) == latest),
        {},
    )
    out["kpi"] = [
        {
            "year": latest[:4],
            "month": latest,
            "ape": latest_v.get("total_ape", ""),
            "vnb": latest_v.get("total_vnb", ""),
            "margin": latest_v.get("total_margin", ""),
        }
    ]

    # channels = latest month (active count + productivity + high-risk churn proxy)
    channels = []
    for r in [x for x in raw["channel_monthly"] if analytics._month_key(x["month"]) == latest]:
        cid = str(r["channel_id"])
        active_here = [
            a
            for a in raw["agent_monthly"]
            if analytics._month_key(a["month"]) == latest
            and str(a["channel_id"]) == cid
            and str(a["status"]).lower() == "active"
        ]
        high = sum(1 for a in active_here if str(a["churn_risk"]) == "High")
        churn = (high / len(active_here)) if active_here else 0.0
        channels.append(
            {
                "id": "Channel:" + cid,
                "name": r["channel_name"],
                "count": int(analytics._num(r["active"])),
                "churn": round(churn, 4),
                "prod": round(analytics._num(r["avg_prod"]), 3),
                "ape": round(analytics._num(r["ape"]), 3),
                "blended": round(analytics._num(r["margin"]), 4),
                "vnb": round(analytics._num(r["vnb"]), 3),
            }
        )
    channels.sort(key=lambda x: x["id"])
    out["channels"] = channels

    # mix = latest month
    out["mix"] = [
        {
            "channel_id": "Channel:" + str(r["channel_id"]),
            "product_id": "Product:" + str(r["product_id"]),
            "weight": r["mix"],
            "year": latest[:4],
        }
        for r in raw["mix_monthly"]
        if analytics._month_key(r["month"]) == latest
    ]

    # agents = latest monthly status
    out["agents"] = [
        {
            "id": "Agent:" + str(r["agent_id"]),
            "channel_id": "Channel:" + str(r["channel_id"]),
            "tenure": int(analytics._num(r["tenure"])) if r["tenure"] != "" else "",
            "productivity": round(analytics._num(r["prod"]), 2),
            "certification": r["certification"],
            "churn_risk": r["churn_risk"],
            "status": r["status"],
        }
        for r in raw["agent_monthly"]
        if analytics._month_key(r["month"]) == latest
    ]

    out["__raw_tables__"] = raw_defs
    return out


def _load_snapshot_pack(wb: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Original single-snapshot workbook loader (fallback for non-monthly books)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    sheet_rows = _make_sheet_rows(wb)

    prod_rows = sheet_rows(
        "Product",
        {
            "id": "product_id",
            "name": "product_name",
            "type": "product_type",
            "margin": "VNB_margin",
            "comm": "commission_rate",
            "exp": "expense_ratio",
        },
    )
    for r in prod_rows:
        r["id"] = "Product:" + str(r["id"])
    out["products"] = prod_rows
    out["kpi"] = sheet_rows(
        "VNB Summary",
        {
            "year": "Year",
            "ape": "Total APE ($M)",
            "vnb": "Total VNB ($M)",
            "margin": "VNB Margin",
        },
    )
    ch_rows = sheet_rows(
        "Distribution Channel",
        {
            "id": "channel_id",
            "name": "channel_name",
            "count": "agent_count",
            "churn": "agent_churn_rate",
            "prod": "avg_productivity",
        },
    )
    for r in ch_rows:
        r["id"] = "Channel:" + str(r["id"])
        p = r.get("prod")
        if isinstance(p, str):
            r["prod"] = int(re.sub(r"[^0-9]", "", p) or 0)
    out["channels"] = ch_rows
    ag_rows = sheet_rows(
        "Agent",
        {
            "id": "agent_id",
            "channel_id": "channel_id",
            "tenure": "tenure_months",
            "productivity": "productivity",
            "certification": "certification",
            "churn_risk": "churn_risk",
        },
    )
    for r in ag_rows:
        r["channel_id"] = "Channel:" + str(r["channel_id"])
        p = r.get("productivity")
        if isinstance(p, str):
            r["productivity"] = int(re.sub(r"[^0-9]", "", p) or 0)
    out["agents"] = ag_rows
    mix_rows = sheet_rows(
        "Product Mix",
        {
            "channel_id": "channel_id",
            "product_id": "product_id",
            "weight": "product_mix",
        },
    )
    for r in mix_rows:
        r["channel_id"] = "Channel:" + str(r["channel_id"])
        r["product_id"] = "Product:" + str(r["product_id"])
        r["year"] = 2025
    out["mix"] = mix_rows
    # snapshot books: derive channel APE / blended margin / VNB in Python.
    pmargin = {p["id"]: p["margin"] for p in prod_rows}
    for ch in ch_rows:
        cid = ch["id"]
        ch["ape"] = round(ch["count"] * ch["prod"] / 1000.0, 2)
        ws = [w for w in mix_rows if w["channel_id"] == cid]
        den = sum(float(w["weight"]) for w in ws) or 1.0
        ch["blended"] = round(
            sum(float(w["weight"]) * float(pmargin.get(w["product_id"], 0)) for w in ws) / den,
            4,
        )
        ch["vnb"] = round(ch["ape"] * ch["blended"], 3)
    return out


# Sheets that identify a **monthly** workbook (vs the original single-snapshot one).
MONTHLY_SHEET_SIGNATURE = ("VNB Monthly", "Agent Monthly")


def _discover_workbook() -> Optional[Path]:
    """Find a sample workbook without hardcoding a filename.

    Look-up order:
    1. An explicit path supplied via the ``ONTI_DATA_WORKBOOK`` environment
       variable (absolute, or relative to the project root).
    2. Any ``.xlsx`` file (excluding Excel temp files ``~$...``) under the
       project root or ``data/``, preferring one that carries the monthly
       sheets so time-series analytics light up.

    Returns ``None`` when no workbook is present — the pack then falls back to
    the in-code seed rows.
    """
    explicit = os.environ.get("ONTI_DATA_WORKBOOK")
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = BASE_DIR.parent / p
        if p.exists():
            return p

    search_dirs = [BASE_DIR.parent, BASE_DIR / "data"]
    seen: set = set()
    xlsx_files: List[Path] = []
    for d in search_dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*.xlsx"):
            if p.name.startswith("~$"):
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            xlsx_files.append(p)
    if not xlsx_files:
        return None

    try:
        import openpyxl  # noqa: WPS433
    except ImportError:
        return xlsx_files[0]

    # Prefer a workbook with the monthly sheets; otherwise take the first one.
    for p in xlsx_files:
        try:
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        except Exception:  # noqa: BLE001
            continue
        try:
            names = wb.sheetnames
        finally:
            wb.close()
        if all(s in names for s in MONTHLY_SHEET_SIGNATURE):
            return p
    return xlsx_files[0]


def _load_excel_rows() -> Dict[str, List[Dict[str, Any]]]:
    """Read the sample workbook (auto-discovered; monthly pack preferred)."""
    xlsx_path = _discover_workbook()
    if xlsx_path is None:
        return {}
    try:
        import openpyxl  # noqa: WPS433
    except ImportError:
        return {}
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    if all(s in wb.sheetnames for s in MONTHLY_SHEET_SIGNATURE):
        return _load_monthly_pack(wb)
    return _load_snapshot_pack(wb)


def _build_tables() -> List[Dict[str, Any]]:
    """Return TABLES with rows replaced by live Excel data; append raw tables."""
    excel = _load_excel_rows()
    raw_defs = excel.pop("__raw_tables__", []) if excel else []
    built = []
    for tbl in TABLES:
        t = dict(tbl)
        if tbl["key"] in excel and excel[tbl["key"]]:
            t["rows"] = excel[tbl["key"]]
        built.append(t)
    existing = {t["key"] for t in built}
    for rd in raw_defs:
        if rd["key"] not in existing:
            built.append(rd)
    return built


def _graph_node_value_overrides() -> Dict[str, str]:
    """Latest-month display values for graph nodes, keyed by node key."""
    excel = _load_excel_rows()
    raw = {d["key"]: d["rows"] for d in excel.get("__raw_tables__", [])}
    if not raw.get("vnb_monthly"):
        return {}
    from . import analytics

    comp = analytics.company_series(raw)
    if not comp:
        return {}
    co = comp[-1]
    month = co["month"]
    chan = {c["channel_id"]: c for c in analytics.channel_series(raw) if c["month"] == month}
    order = ["CH001", "CH002", "CH003"]
    present = [c for c in order if c in chan]
    agency = chan.get("CH001")

    # High churn-risk share among each channel's active agents (latest month).
    churn_share: Dict[str, float] = {}
    am = [r for r in raw.get("agent_monthly", []) if analytics._month_key(r.get("month")) == month]
    for cid in order:
        active = [r for r in am if str(r.get("channel_id")) == cid and str(r.get("status")).lower() == "active"]
        churn_share[cid] = sum(1 for r in active if str(r.get("churn_risk")) == "High") / len(active) if active else 0.0

    ov: Dict[str, str] = {
        "TotalVNB": f"${co['total_vnb']:.2f}M",
        "TotalAPE": f"${co['total_ape']:.2f}M",
        "BlendedVNBMargin": f"{co['margin'] * 100:.1f}%",
        "ChannelAPE": " / ".join(f"${chan[c]['ape']:.2f}M" for c in present),
        "ChannelBlendedMargin": " / ".join(f"{chan[c]['margin'] * 100:.1f}%" for c in present),
        "AgentCount": " / ".join(str(int(chan[c]["active"])) for c in present),
        "AgentProductivity": " / ".join(f"${chan[c]['avg_prod']:.1f}K" for c in present),
        "AgentChurnRate": " / ".join(f"{churn_share[c] * 100:.0f}%" for c in present),
    }
    if agency:
        ov.update(
            {
                "ActiveAgentCount": str(int(agency["active"])) + " Agency active",
                "AgentStatus": f"{int(agency['active'])}/{int(agency['registered'])} active",
                "AgencyAPE": f"${agency['ape']:.2f}M",
                "AgencyVNB": f"${agency['vnb']:.2f}M",
                "WeightedVNBMargin": f"{agency['margin'] * 100:.1f}%",
            }
        )
    # entity-level nodes (monthly data tables)
    ch_ids = {str(r.get("channel_id")) for r in raw.get("channel_monthly", [])}
    mix_rows = [r for r in raw.get("mix_monthly", []) if analytics._month_key(r.get("month")) == month]
    ov.update(
        {
            "VnbMonthly": f"{len(comp)} months · latest {month}",
            "ChannelMonthly": f"{len(ch_ids)} channels · {month}",
            "AgentMonthly": f"{len(am)} agents · {month}",
            "ProductMixMonthly": f"{len(mix_rows)} product rows · {month}",
            "ScenarioAnalysis": "target back-solve",
        }
    )
    return ov


def ensure_seed(conn: sqlite3.Connection) -> int:
    """Create the built-in sample pack once. Returns its pack id."""
    row = conn.execute("SELECT id FROM ontology_packs WHERE slug=?", (PACK_SLUG,)).fetchone()
    if row:
        pack_id = row["id"]
        if not get_setting(conn, "active_pack_id"):
            set_setting(conn, "active_pack_id", pack_id)
        upgrade_sample_pack(conn, pack_id)
        return pack_id

    pack_id = conn.execute(
        "INSERT INTO ontology_packs(slug, name, description, schema_version, "
        "narrative, is_builtin, is_active, created_at) VALUES(?,?,?,?,?,?,1,?)",
        (PACK_SLUG, PACK_NAME, PACK_DESC, "onti-pack/v1", NARRATIVE, 1, now()),
    ).lastrowid

    # ---- formal schema ------------------------------------------------------
    schema = _load_formal_schema()
    for i, ent in enumerate(schema.get("entities", []), 1):
        conn.execute(
            "INSERT INTO entities(pack_id, key, label_en, layer, description, sort) " "VALUES(?,?,?,?,?,?)",
            (
                pack_id,
                ent["id"],
                ent.get("label_en", ent["id"]),
                ent.get("layer", 3),
                ent.get("description", ""),
                i,
            ),
        )
    for i, att in enumerate(schema.get("attributes", []), 1):
        conn.execute(
            "INSERT INTO attributes(pack_id, entity_key, key, label_en, data_type, "
            "unit, enum_values, required, is_unique, description, sort) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                pack_id,
                att.get("owned_class", ""),
                att["id"],
                att.get("label_en", att["id"]),
                att.get("data_type", "STRING"),
                att.get("unit", ""),
                att.get("enum_values", ""),
                int(att.get("required", 0) or 0),
                int(att.get("unique", 0) or 0),
                att.get("description", ""),
                i,
            ),
        )
    for i, rel in enumerate(schema.get("relations", []), 1):
        conn.execute(
            "INSERT INTO relations(pack_id, key, label_en, cardinality, source_key, "
            "target_key, source_attr, target_attr, description, sort) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                pack_id,
                rel["id"],
                rel.get("label_en", ""),
                rel.get("cardinality", "MANY_TO_MANY"),
                rel.get("source_class", ""),
                rel.get("target_class", ""),
                rel.get("source_class_attribute", ""),
                rel.get("target_class_attribute", ""),
                rel.get("description", ""),
                i,
            ),
        )

    # ---- graph nodes / edges ------------------------------------------------
    _insert_seed_graph(conn, pack_id)

    # ---- metrics ------------------------------------------------------------
    for i, met in enumerate(METRICS, 1):
        conn.execute(
            "INSERT INTO metric_formulas(pack_id, key, label, expression, unit, fmt, "
            "decimals, sort) VALUES(?,?,?,?,?,?,?,?)",
            (
                pack_id,
                met["key"],
                met["label"],
                met["expression"],
                met["unit"],
                met["fmt"],
                met.get("decimals", 2),
                i,
            ),
        )

    # ---- rules + canonical question bank ------------------------------------
    for r in RULES:
        conn.execute(
            "INSERT INTO question_rules(pack_id, key, category, patterns, "
            "require_groups, guards, template, evidence_nodes, path_steps, sort) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                pack_id,
                r["key"],
                r["category"],
                jdumps(r["patterns"]),
                jdumps(r["require_groups"]),
                jdumps(r["guards"]),
                r["template"],
                jdumps(r["nodes"]),
                jdumps(r["path"]),
                r["sort"],
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO question_bank(pack_id, question, norm, category, "
            "source, created_at) VALUES(?,?,?,?,?,?)",
            (
                pack_id,
                r["question"],
                r["question"].strip().lower(),
                r["category"],
                "canonical",
                now(),
            ),
        )

    # ---- instance tables / columns / seed datasets --------------------------
    for ti, tbl in enumerate(_build_tables(), 1):
        cur = conn.execute(
            "INSERT INTO pack_tables(pack_id, key, label, entity_key, description) " "VALUES(?,?,?,?,?)",
            (
                pack_id,
                tbl["key"],
                tbl["label"],
                tbl.get("entity_key", ""),
                tbl.get("description", ""),
            ),
        )
        table_id = cur.lastrowid
        for ci, col in enumerate(tbl["columns"], 1):
            conn.execute(
                "INSERT INTO pack_columns(table_id, key, label, data_type, unit, "
                "enum_values, expression, fmt, decimals, required, sort) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    table_id,
                    col["key"],
                    col.get("label", col["key"]),
                    col.get("data_type", "STRING"),
                    col.get("unit", ""),
                    col.get("enum_values", ""),
                    col.get("expression", ""),
                    col.get("fmt", "raw"),
                    col.get("decimals", 2),
                    int(col.get("required", 0)),
                    ci,
                ),
            )
        ds = conn.execute(
            "INSERT INTO datasets(pack_id, table_id, name, filename, row_count, "
            "kind, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                pack_id,
                table_id,
                f"Seed: {tbl['label']}",
                "",
                len(tbl["rows"]),
                "seed",
                now(),
            ),
        ).lastrowid
        for seq, row_data in enumerate(tbl["rows"]):
            conn.execute(
                "INSERT INTO pack_rows(dataset_id, seq, data) VALUES(?,?,?)",
                (ds, seq, jdumps(row_data)),
            )
        conn.execute(
            "UPDATE pack_tables SET active_dataset_id=? WHERE id=?",
            (ds, table_id),
        )

    if not get_setting(conn, "active_pack_id"):
        set_setting(conn, "active_pack_id", pack_id)
    audit(
        conn,
        "system",
        "seed.pack_created",
        f"Built-in sample pack materialized (pack_id={pack_id})",
    )
    upgrade_sample_pack(conn, pack_id)
    return pack_id


def _insert_seed_graph(conn: sqlite3.Connection, pack_id: int) -> None:
    """Insert the rich KPI-chain graph (NODES + EDGES) plus the entity-level
    monthly-table nodes/edges for a pack. Used by ensure_seed (fresh DBs) and
    by upgrade_sample_pack (repairing a degraded graph)."""
    node_overrides = _graph_node_value_overrides()
    base = conn.execute("SELECT COUNT(*) AS c FROM graph_nodes WHERE pack_id=?", (pack_id,)).fetchone()["c"]
    all_nodes = NODES + UPGRADE_ENTITY_NODES
    all_edges = EDGES + UPGRADE_GRAPH_EDGES
    for i, node in enumerate(all_nodes, base + 1):
        if conn.execute(
            "SELECT 1 FROM graph_nodes WHERE pack_id=? AND key=?",
            (pack_id, node["key"]),
        ).fetchone():
            continue
        conn.execute(
            "INSERT INTO graph_nodes(pack_id, key, grp, value, description, aliases, "
            "is_core, sort) VALUES(?,?,?,?,?,?,?,?)",
            (
                pack_id,
                node["key"],
                node["grp"],
                node_overrides.get(node["key"], node["value"]),
                node["desc"],
                jdumps(node["aliases"]),
                node["core"],
                i,
            ),
        )
    ebase = conn.execute("SELECT COUNT(*) AS c FROM graph_edges WHERE pack_id=?", (pack_id,)).fetchone()["c"]
    for i, e in enumerate(all_edges, ebase + 1):
        if conn.execute("SELECT 1 FROM graph_edges WHERE pack_id=? AND key=?", (pack_id, e["key"])).fetchone():
            continue
        conn.execute(
            "INSERT INTO graph_edges(pack_id, key, source_key, target_key, edge_type, "
            "label, description, sort) VALUES(?,?,?,?,?,?,?,?)",
            (
                pack_id,
                e["key"],
                e["src"],
                e["tgt"],
                e["type"],
                e["label"],
                e["desc"],
                i,
            ),
        )


def upgrade_sample_pack(conn: sqlite3.Connection, pack_id: int) -> None:
    """Idempotent v2 upgrade for the built-in sample pack.

    1. Adds the monthly data-table entities (VNB Monthly, Channel Monthly,
       Agent Monthly, Product Mix Monthly, Scenario Analysis) with attributes
       and relations, so the ontology covers every knowledge-base table.
    2. Repairs a degraded impact graph: if the pack's graph only holds the
       5 auto-generated entity nodes (values wiped by a schema sync), the
       rich KPI causal chain is restored.
    3. Points the scenario_data table at the new scenario_analysis entity.
    """
    flag = f"sample_pack_upgraded_v2:{pack_id}"
    if get_setting(conn, flag):
        return
    row = conn.execute("SELECT slug FROM ontology_packs WHERE id=?", (pack_id,)).fetchone()
    if row is None or row["slug"] != PACK_SLUG:
        return

    # 1. entities + attributes
    for si, ent in enumerate(UPGRADE_ENTITIES, 1):
        exists = conn.execute("SELECT 1 FROM entities WHERE pack_id=? AND key=?", (pack_id, ent["key"])).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO entities(pack_id, key, label_en, layer, description, " "sort) VALUES(?,?,?,?,?,?)",
                (pack_id, ent["key"], ent["label"], ent["layer"], ent["desc"], si),
            )
        for ai, (ak, alabel, atype, aunit) in enumerate(ent["attrs"], 1):
            conn.execute(
                "INSERT OR IGNORE INTO attributes(pack_id, entity_key, key, "
                "label_en, data_type, unit, enum_values, required, is_unique, "
                "description, sort) VALUES(?,?,?,?,?,?,?,0,0,'',?)",
                (pack_id, ent["key"], ak, alabel, atype, aunit, "", ai),
            )

    # 2. relations
    for si, rel in enumerate(UPGRADE_RELATIONS, 1):
        conn.execute(
            "INSERT OR IGNORE INTO relations(pack_id, key, label_en, cardinality, "
            "source_key, target_key, source_attr, target_attr, description, sort) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                pack_id,
                rel["key"],
                rel["label"],
                rel["card"],
                rel["src"],
                rel["tgt"],
                rel["src_attr"],
                rel["tgt_attr"],
                rel["desc"],
                si,
            ),
        )

    # 3. repair a degraded impact graph (only entity nodes, no values)
    degraded = (
        conn.execute("SELECT COUNT(*) AS c FROM graph_nodes WHERE pack_id=?", (pack_id,)).fetchone()["c"]
        <= len(UPGRADE_ENTITY_NODES) + 5
        and conn.execute(
            "SELECT 1 FROM graph_nodes WHERE pack_id=? AND key='TotalVNB'",
            (pack_id,),
        ).fetchone()
        is None
    )
    if degraded:
        conn.execute("DELETE FROM graph_edges WHERE pack_id=?", (pack_id,))
        conn.execute("DELETE FROM graph_nodes WHERE pack_id=?", (pack_id,))
    _insert_seed_graph(conn, pack_id)

    # 4. legacy scenario_data rows point at the new entity
    conn.execute(
        "UPDATE pack_tables SET entity_key='scenario_analysis' "
        "WHERE pack_id=? AND key='scenario_data' AND entity_key=''",
        (pack_id,),
    )

    set_setting(conn, flag, now())
    audit(
        conn,
        "system",
        "seed.pack_upgraded",
        f"Sample pack upgraded to v2: monthly-table entities + rich graph " f"(pack_id={pack_id})",
    )


# ===========================================================================
# Optional .env model bootstrap (runs once, when no model is configured)
# ===========================================================================


def _env_value(raw: str) -> str:
    """``KEY='value'`` / ``KEY="value"`` → ``value``, the way dotenv reads it."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def migrate_legacy_model() -> None:
    """Import a default model from .env / environment on first startup.

    Reads the provider-neutral ``LLM_API_KEY`` / ``LLM_BASE_URL`` /
    ``LLM_MODEL_NAME`` variables. The legacy ``SILICONFLOW_*`` names are still
    honoured for backward compatibility.

    Self-signed gateways (Deloitte Nova, on-prem proxies) can set
    ``LLM_VERIFY_SSL=false`` / ``NOVA_VERIFY_SSL=false`` /
    ``SKIP_SSL_VERIFY=1`` to flip the verify flag.

    Values are normalised exactly like a key typed into the admin form: a
    ``.env`` line such as ``SILICONFLOW_API_KEY='sk-…'`` carries the quote
    characters into the database otherwise, and every later call would be
    rejected with ``Token is invalid`` although the key itself is good.
    """
    from modules.llm_client import normalize_api_key, normalize_base_url

    env_path = BASE_DIR / ".env"
    values: Dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = _env_value(v)
    values.update({k: v for k, v in os.environ.items() if k.startswith(("LLM_", "SILICONFLOW_", "NOVA_"))})

    api_key = normalize_api_key(values.get("LLM_API_KEY") or values.get("SILICONFLOW_API_KEY", ""))
    if not api_key:
        return
    base_url = normalize_base_url(
        values.get("LLM_BASE_URL") or values.get("SILICONFLOW_BASE_URL") or "https://api.openai.com/v1"
    )
    model_name = (values.get("LLM_MODEL_NAME") or values.get("SILICONFLOW_MODEL_NAME", "")).strip()
    if not model_name:
        return

    # Self-signed gateway support: any of these truthy values flips verify off.
    # `SKIP_SSL_VERIFY` is a standalone trigger — its mere presence means skip
    # (even when set to "0"), because it was designed as a standalone toggle.
    has_skip_trigger = bool(values.get("SKIP_SSL_VERIFY"))
    verify_raw = (
        (values.get("LLM_VERIFY_SSL") or values.get("NOVA_VERIFY_SSL") or values.get("SILICONFLOW_VERIFY_SSL", ""))
        .strip()
        .lower()
    )
    if has_skip_trigger:
        verify_ssl = False
    elif not verify_raw:
        verify_ssl = True
    elif verify_raw in {"0", "false", "no", "n", "off"}:
        verify_ssl = False
    elif verify_raw in {"1", "true", "yes", "y", "on", "skip"}:
        verify_ssl = verify_raw != "skip"
    else:
        verify_ssl = True

    with get_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM model_configs").fetchone()["n"]
        if existing:
            return
        conn.execute(
            "INSERT INTO model_configs(name, base_url, api_key_enc, model, "
            "temperature, max_tokens, timeout, verify_ssl, enabled, is_default, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,1,?,?)",
            (
                "Default model (migrated from .env)",
                base_url,
                encrypt_secret(api_key),
                model_name,
                0.3,
                1024,
                90,
                1 if verify_ssl else 0,
                1,
                now(),
            ),
        )
        audit(
            conn,
            "system",
            "model.migrated",
            "Default model imported from .env bootstrap",
        )
