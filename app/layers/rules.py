"""确定性规则集中放在这里，便于单独测试和审阅。"""
from __future__ import annotations

import re

from app.models import FIRST_HAND, ClaimType

ANON = r"据知情人士|知情人士透露|据悉|网传|据传|传闻称|市场人士称|消息人士|小作文|业内人士透露"
FUTURE = r"拟|计划|预计|将于|有望|争取|筹划中|预期"
OPINION = r"认为|看好|判断|建议|评级|预测"
DENY = r"不实|不存在|未筹划|没有筹划|否认|谣言|不属实|与事实不符|未发生|并无|没有任何|子虚乌有|造谣"
CONFIRM = r"属实|确认|已签署|已完成|已收到|正在推进"
BOILER = (r"^(特此公告|敬请广大投资者|公司郑重提醒|本公司及董事会全体成员|公司指定信息披露媒体|.{0,8}董事会$)"
          r"|证券代码[:：]|保证信息披露的内容真实|投资者.{0,12}(理性|审慎|谨慎)|注意(相关|投资)?风险"
          r"|切勿轻信|持续关注.{0,20}(履行|披露)|以.{0,30}正式公告为准|不信谣|感谢.{0,6}关注")
HEADING = r"^([一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+[.．、])(传闻情况说明|澄清声明|其他说明|情况说明|核查情况|风险提示|[^，。：:]{2,8}[:：])?\s*"
# 只有与事件核心要素相关的一手节点，才能把事件推向“进行中 / 已完成 / 已披露预案”
CORE_STATES = {"进行中", "已完成", "已披露预案"}
STAGES = ["公司已否认", "公司已回应", "已披露预案", "进行中", "已完成", "已延期", "已终止", "已更正", "无"]
# 同一节点出现多个阶段时取最重要的一个
STAGE_PRIORITY = ["已更正", "已终止", "已延期", "公司已否认", "已完成", "进行中", "已披露预案", "公司已回应"]
CUES = DENY + "|" + CONFIRM + r"|澄清|传闻|言论|报道|处罚|报警|核查|进展|签署|终止|更正|收购|减持|增持|中标|合同|更正前|更正后|原披露|调整为|延期|推迟|预定可使用状态"
# 公告里的合规套话：说的是“没有违规”，不是在否认用户关心的那件事
COMPLIANCE_BOILER = r"不存在(损害|变相改变|改变或变相|违反|影响募集资金|其他影响|重大遗漏|虚假)|符合.{0,20}(规定|要求)|不会对公司.{0,12}(产生重大|造成重大)"
# 只有这些对象才算“传闻的出处”；其他被 LLM 标成转述的，按公司自身表述处理
RUMOR_SPEAKER = r"媒体|报道|网络|网传|网上|市场|传闻|社区|平台|自媒体|论坛|微信|微博|股吧|知情人士|消息人士|投资者"

# 保守程度：越靠前越保守
CONSERVATIVE = ["传闻", "推测", "观点", "事实"]

# 能改变事件状态的规则：(正则, 状态)。只对一手来源生效。
STATE_RULES = [
    (r"更正", "已更正"),
    (r"终止|取消|撤回", "已终止"),
    (r"延期|推迟|未能按期|不能按期", "已延期"),
    (DENY, "公司已否认"),
    (r"已完成|完成过户|实施完毕|办理完毕", "已完成"),
    (r"进展|审议通过|获得批准|已签署|正在推进|实施", "进行中"),
    (r"拟|预案|框架协议|计划", "已披露预案"),
    (CONFIRM, "公司已回应"),
]


def allowed_types(source_type: str, quote: str, attributed_to: str) -> list[ClaimType]:
    """硬规则：按“谁说的”圈定一条声明可以是哪几类。

    匿名信源规则看的是声明的说话人：公告转述的传闻（attributed_to 非空）只能是传闻；
    公司自己否认传闻的句子即使提到“网传”，说话人仍是公司，不受此规则约束。
    """
    if attributed_to:
        return ["传闻"]
    if source_type == "selfmedia":
        return ["传闻"]
    if source_type not in FIRST_HAND and re.search(ANON, quote):
        return ["传闻"]
    if source_type in FIRST_HAND:
        return ["事实", "推测"]
    if source_type == "news":
        return ["事实", "观点", "推测"]
    if source_type == "research":
        return ["观点", "推测"]
    return ["传闻"]


def most_conservative(types: list[str]) -> ClaimType:
    return min(types, key=CONSERVATIVE.index)  # type: ignore[return-value]


def cue_type(allowed: list[ClaimType], quote: str) -> ClaimType:
    """LLM 不可用时的确定性判定：只在可选范围内，按措辞线索取值。"""
    if "推测" in allowed and re.search(FUTURE, quote):
        return "推测"
    if "观点" in allowed and re.search(OPINION, quote):
        return "观点"
    if "事实" in allowed:
        return "事实"
    return most_conservative(list(allowed))


def state_effect(text: str) -> str | None:
    for pat, state in STATE_RULES:
        if re.search(pat, text):
            return state
    return None


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。；！？])|(?<=[：:])(?=\d+[.．、])", text)
    out = []
    for p in parts:
        p = p.strip()
        # 公告首句常带着版头（证券代码、标题），切掉版头，保留原文中的正文部分
        if "证券代码" in p and "（以下简称“公司”）" in p:
            p = p.split("（以下简称“公司”）", 1)[1]
        p = re.sub(HEADING, "", p)
        if len(p) >= 6:
            out.append(p)
    return out
