# -*- coding: utf-8 -*-
"""adclean —— 中文小说 TXT 广告（QQ群/TG频道）校正引擎。

Auto Repair Engine（规则引擎）+ Audit/Review（高召回审查、区域分级、LLM 疑难交接）。
"""
from . import charclass  # noqa: F401
from .engine import (  # noqa: F401
    Model,
    Config,
    LineResult,
    TextResult,
    Edit,
    clean_line,
    clean_text,
    contaminated_predicate,
)
from .audit import (  # noqa: F401
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    AuditConfig,
    AuditEvent,
    AuditLayer,
    Evidence,
)
from .review import (  # noqa: F401
    DECISION_KEEP,
    DECISION_REPAIR,
    DECISION_UNCERTAIN,
    CallableResolver,
    CandidateRepair,
    MappingResolver,
    NullResolver,
    ReviewItem,
    apply_decision,
    build_prompt,
    parse_decision,
)
from .pipeline import (  # noqa: F401
    PipelineConfig,
    PipelineResult,
    run_book,
    run_files,
)

__all__ = [
    # Auto Repair
    "Model", "Config", "LineResult", "TextResult", "Edit",
    "clean_line", "clean_text", "contaminated_predicate", "charclass",
    # Audit
    "AuditLayer", "AuditConfig", "AuditEvent", "Evidence",
    "SEV_HIGH", "SEV_MEDIUM", "SEV_LOW",
    # Review
    "ReviewItem", "CandidateRepair", "NullResolver", "MappingResolver",
    "CallableResolver", "build_prompt", "parse_decision", "apply_decision",
    "DECISION_REPAIR", "DECISION_KEEP", "DECISION_UNCERTAIN",
    # Pipeline
    "PipelineConfig", "PipelineResult", "run_book", "run_files",
]

__version__ = "0.2.0"
