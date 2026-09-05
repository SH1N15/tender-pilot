"""Compatibility imports for legacy route contracts during G-4.

Business routers import this module only; capability implementations remain owned by
graph nodes and existing skills while G-5 migrates frontend callers.
"""

from services.check.skills.ai_text_check_skill import AITextCheckSkill
from services.check.skills.check_report_export_skill import CheckReportExportSkill
from services.check.skills.compliance_check_skill import ComplianceCheckSkill
from services.check.skills.consistency_check_skill import ConsistencyCheckSkill
from services.check.skills.cross_check_skill import CrossCheckSkill
from services.check.skills.deposit_check_skill import DepositCheckSkill
from services.check.skills.disqualification_check_skill import DisqualificationCheckSkill
from services.check.skills.doc_integrity_check_skill import DocIntegrityCheckSkill
from services.check.skills.duplicate_check_skill import DuplicateCheckSkill
from services.check.skills.ebid_submit_check_skill import EbidSubmitCheckSkill
from services.check.skills.fit_score_skill import FitScoreSkill
from services.check.skills.joint_bid_check_skill import JointBidCheckSkill
from services.check.skills.mandatory_req_check_skill import MandatoryReqCheckSkill
from services.check.skills.pricing_check_skill import PricingCheckSkill
from services.check.skills.pricing_logic_check_skill import PricingLogicCheckSkill
from services.check.skills.qualification_check_skill import QualificationCheckSkill
from services.check.skills.risk_score_skill import RiskScoreSkill
from services.check.skills.sample_report_check_skill import SampleReportCheckSkill
from services.check.skills.selfcheck_list_skill import SelfcheckListSkill
from services.check.skills.signature_check_skill import SignatureCheckSkill
from services.check.skills.validity_check_skill import ValidityCheckSkill
from services.check.skills.whitelist_filter_skill import WhitelistFilterSkill
from services.generate.skills.content_gen_skill import ContentGenSkill
from services.generate.skills.mandatory_req_extract_skill import MandatoryReqExtractSkill
from services.generate.skills.outline_gen_skill import OutlineGenSkill
from services.generate.skills.structure_template_skill import ScoreCoverageSkill, StructureTemplateSkill
from services.interpret.skills.interpret_export_skill import InterpretExportSkill
from services.interpret.skills.risk_alert_skill import RiskAlertSkill
from services.interpret.skills.scoring_matrix_skill import ScoringMatrixSkill
from services.interpret.skills.tender_interpret_skill import TenderInterpretSkill, merge_dimensions
from services.qualification.workflow import approve_qualification_workflow

_EXPORTED = (
    AITextCheckSkill, CheckReportExportSkill, ComplianceCheckSkill, ConsistencyCheckSkill,
    CrossCheckSkill, DepositCheckSkill, DisqualificationCheckSkill, DocIntegrityCheckSkill,
    DuplicateCheckSkill, EbidSubmitCheckSkill, FitScoreSkill, JointBidCheckSkill,
    MandatoryReqCheckSkill, PricingCheckSkill, PricingLogicCheckSkill, QualificationCheckSkill,
    RiskScoreSkill, SampleReportCheckSkill, SelfcheckListSkill, SignatureCheckSkill,
    ValidityCheckSkill, WhitelistFilterSkill, ContentGenSkill, OutlineGenSkill,
    ScoreCoverageSkill, StructureTemplateSkill, MandatoryReqExtractSkill,
    InterpretExportSkill, RiskAlertSkill,
    ScoringMatrixSkill, TenderInterpretSkill, merge_dimensions, approve_qualification_workflow,
)
__all__ = [
    name for name in globals()
    if name.endswith("Skill") or name in {"merge_dimensions", "approve_qualification_workflow"}
]
