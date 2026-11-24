"""Question-specific factual boundaries and engineering checks, carried as data."""

import re


PREFERENCE_FIELDS = {
    "salary": (r"薪资|薪酬|薪水|待遇|salary|compensation", "期望薪资"),
    "overtime": (r"加班|overtime", "加班接受范围"),
    "availability": (r"到岗|入职时间|何时入职|什么时候入职|notice period|available to start", "到岗时间"),
}
_STATES = {"confirmed", "source_supported", "unknown", "conflict", "excluded", "planned", "needs_confirmation"}


def requested_preferences(question):
    if not re.search(r"你|您|我|期望薪|到岗时间|是否接受|能否接受|your|can you|would you", question, re.I):
        return []
    if re.search(r"加班费|劳动法|如何计算|怎么算|招聘系统|设计.*(?:薪资|薪酬)", question):
        return []
    fields = [field for field, (pattern, _) in PREFERENCE_FIELDS.items() if re.search(pattern, question, re.I)]
    if "overtime" in fields and not re.search(r"接受|愿意|能否|能不能|是否可以|可不可以|能加班|可以加班|can you|would you", question, re.I):
        fields.remove("overtime")
    return fields


def fact_contract(question, reference):
    raw = list(reference.get("candidate_facts", []))
    for item in reference.get("evidence", []):
        checks = ([item["fact_check"]] if item.get("fact_check") else []) + item.get("fact_checks", [])
        raw.extend({"source": item.get("source"), "lines": item.get("lines"), **check} for check in checks)
    records = []
    for fact in raw:
        if not isinstance(fact, dict) or fact.get("state") not in _STATES:
            continue
        records.append({key: fact[key] for key in ("subject", "field", "value", "state", "source", "lines") if key in fact})
    preferences = {}
    for field in requested_preferences(question):
        matches = [fact for fact in records if fact.get("field") == field
                   and fact.get("subject") in {"本人", "候选人", "我", "candidate", "self"}]
        states = {fact["state"] for fact in matches}
        state = next((value for value in ("conflict", "confirmed", "excluded", "needs_confirmation", "planned", "source_supported")
                      if value in states), "unknown")
        preferences[field] = {"state": state}
    implementation = None
    target = (re.search(r"(?:实际落地|实际实施|独立上线过)(.{2,45}?)(?:的过程|的经历|吗|么|？|\?|。|$)", question)
              or re.search(r"(?:做过|使用过)([^的什么哪怎么如何，,。]{2,30}?)(?:吗|么|？|\?)", question))
    if target and re.search(r"你|您|本人|我", question):
        subject = target[1].strip(' ，,。？?')
        personal_text = '\n'.join([reference.get('resume', '')] + [
            item.get('excerpt', '') for item in reference.get('evidence', [])
            if item.get('kind') == 'candidate_material'])
        supported = any(subject in clause and re.search(r"负责|实现|落地|实施|参与|开发|上线|做过|使用|没有|未曾", clause)
                        for clause in re.split(r"[。\n]", personal_text))
        supported |= any(subject in str(fact.get('value', '')) and fact['state'] in {'confirmed', 'source_supported'}
                         for fact in records)
        implementation = {"subject": subject, "state": "source_supported" if supported else "unknown"}
    resume = reference.get('resume', '')
    graduation_context = ' '.join(re.findall(r'(?:毕业时间|毕业日期)[^。\n]{0,100}', resume))
    conflicting_dates = (bool(re.search(r"冲突|不一致|简历甲.*简历乙", resume, re.S))
                         and len(set(re.findall(r"20\d{2}\s*[年./-]\s*\d{1,2}", graduation_context))) > 1)
    return {"missing_state": "unknown", "negative_experience_requires_evidence": True,
            "absence_is_not_denial": True, "speech_is_unconfirmed_self_report": True,
            "preferences": preferences, "records": records,
            "implementation_request": implementation, "graduation_conflict_in_resume": conflicting_dates}


def personal_clarification(reference, question):
    contract = reference.get("fact_contract", {})
    preferences = contract.get("preferences", {})
    if preferences and all(item["state"] not in {"confirmed", "source_supported"} for item in preferences.values()):
        names = "、".join(PREFERENCE_FIELDS[field][1] for field in preferences)
        return "【开口】" + names + "尚未确认，需要按本人实际情况补充。"
    if (re.search(r"毕业.*(?:哪|何时|日期|时间)|(?:哪|何时).*毕业", question)
            and (contract.get('graduation_conflict_in_resume')
                 or any(fact.get("field") == "graduation_date" and fact["state"] == "conflict"
                        for fact in contract.get("records", [])))):
        return "【开口】毕业日期存在冲突，具体日期需要核对；仅凭日期冲突也无法判断当前是否已经毕业。"
    implementation = contract.get('implementation_request')
    if implementation and implementation['state'] == 'unknown':
        return '【开口】当前个人资料未说明“' + implementation['subject'] + '”的实施经历，需要本人补充；未记载不等于没有做过。'
    return None


def paired_error_counts(question):
    match = re.search(r"(?P<order>新旧|旧新)版分别错\s*(\d+)\s*条\s*(?:和|、|与)\s*(\d+)\s*条", question)
    if not match:
        return None
    first, second = int(match[2]), int(match[3])
    new, old = (first, second) if match['order'] == '新旧' else (second, first)
    return {'new_errors': new, 'old_errors': old, 'new_minus_old': new - old}


def numeric_clarification(question):
    counts = paired_error_counts(question)
    if not counts or abs(counts['new_minus_old']) > 1 or not re.search(r'同一批|同批|同一组', question) or '显著' not in question:
        return None
    new, old, difference = counts['new_errors'], counts['old_errors'], counts['new_minus_old']
    direction = '错误数相同' if difference == 0 else ('新版少错' if difference < 0 else '新版多错') + str(abs(difference)) + '条'
    return (f'【开口】不能。这批样本中新版错{new}条、旧版错{old}条，{direction}；样本方向与显著性是两回事。\n'
            '【要点】配对核对 ::: 应核对“旧对新错”和“旧错新对”的逐条交叉计数，使用配对检验；当前零或一条的净差不足以证明显著改善，不能把错误数方向说反。')


_TCP = re.compile(r"\bTCP\b|三次握手|两次握手", re.I)
_TCP_BASICS = re.compile(
    r"(?:(?:换个话题|换一个话题|换一道题|下一个问题)[，,:： ]*)?"
    r"(?:TCP\s*(?:为什么|为何)(?:需要|要|必须)(?:进行)?三次握手"
    r"|(?:为什么|为何)\s*TCP\s*(?:需要|要|必须)(?:进行)?三次握手"
    r"|TCP\s*(?:为什么|为何)(?:不能|不可以)(?:只进行|只做|只用|用)?两次握手)[？?。 ]*",
    re.I,
)


def protocol_answer(question):
    if not _TCP_BASICS.fullmatch(question.strip()):
        return None
    return ("【开口】第三次握手让服务端确认客户端已收到服务端的初始序列号，两次只完成了客户端这一侧的确认。\n"
            "【要点】正常时序 ::: 客户端收到 SYN+ACK 时，已知道自己的 SYN 被确认，也收到了服务端的 SYN。"
            "服务端收到客户端对该 SYN 的有效 ACK 后，才进入 ESTABLISHED。\n"
            "【要点】旧报文边界 ::: 如果旧 SYN 引出与当前连接不符的 SYN+ACK，客户端可用 RST 拒绝这次尝试。")


_CACHE_REFILL = re.compile(r"single[ -]?flight|缓存.*(?:旧值|回填|回源|删|一致)|(?:回填|旧值).*缓存", re.I)
_READ_COMMITTED = re.compile(r"READ[ _-]COMMITTED|读已提交|读提交", re.I)
_ENGINEERING_CHECKS = (
    (r"(?:响应|请求|结果).*(?:乱序|晚到|迟到|覆盖)|(?:搜索框|前端搜索).*连续",
     "取消和防抖可减少开销；用请求序号或查询键限定当前视图，仅接受当前请求的结果、加载和错误状态。"),
    (r"504|(?:订单|表单|提交).*(?:超时|重复点击|连点)|幂等键",
     "前端防连点改善体验。一次业务操作要有可在刷新后恢复的持久身份，服务端用作用域明确的稳定幂等键和唯一约束处理重复提交；超时先查状态。是否能用原键重试、终态失败后要沿用原键还是创建新意图和新键，取决于服务端与下游合约，不能无条件断言。"),
    (r"\bSSE\b|(?:流式|增量).*(?:重连|取消|断线)",
     "区分断线、取消和已提交副作用；用会话和任务身份拒绝旧事件，按游标续传，可用历史不足时用快照恢复。"),
    (r"游标分页|keyset|(?:created_at|时间戳).*(?:游标|分页)",
     "先把完整排序键代入下一页谓词核对方向：ORDER BY key ASC 使用 key > cursor，ORDER BY key DESC 使用 key < cursor；满足该谓词的键在尚未扫描区间，可以进入后续页，不满足的键在已扫描区间。复合唯一键只建立全序，不冻结集合。后插入、更新排序键和删除都可能造成新增、漏读或重复。业务 created_at 上界不能排除后来写入的历史时间数据；固定开始集合需要数据库一致性快照、物化身份集合，或能按可见性/提交版本排除后来写入的水位。"),
    (r"(?:滚动发布|灰度|迁移|回滚).*(?:字段|列|数据库)|(?:字段|列).*(?:改名|删除|删掉|迁移|回滚|滚动发布)",
     "旧版本仍能只写旧字段时，新字段有值也可能落后。保持旧字段为权威读，或同步覆盖所有旧写入口；带版本条件回填，校验后切读，保留回滚窗口再清理。旧列一旦删除，任何仍访问它的版本（包括双写版本）都不能先回滚；必须先恢复兼容 schema，从权威数据或日志恢复可用数据并校验写入一致性，再切换应用。只重新建空列不能恢复丢失或不可逆转换的数据。"),
    (r"\bP(?:95|99)\b.*(?:上升|升高|变慢)|(?:发布|上线).*(?:延迟|变慢|P95|P99)",
     "按版本、路由和状态分层，沿请求链路区分排队、数据库与下游耗时；用对照信号决定下一步。"),
    (r"(?:文件|异步).*任务.*(?:重试|崩溃)|(?:处理|写完)结果.*(?:未更新|崩溃)",
     "按任务身份关联输出与状态；恢复前验输出是否已提交，重试步骤必须可重入，并复现输出提交到状态更新之间的崩溃。"),
)


def reasoning_contract(question, *, referent=""):
    subject = question + "\n" + referent
    checks = []
    if re.search(r"outbox|发消息|消息投递|可靠投递|跨库|不在同库|不同(?:的)?数据库", subject, re.I):
        checks.append("若采用Outbox：业务状态与Outbox记录必须在拥有该业务状态的同一数据库事务提交；发送成功、标记完成前崩溃，恢复重试可能重发，消费者的去重记录与业务变更也应在其本地事务原子提交。最终收敛依赖持续重试、依赖可用性、幂等处理和对账，不能写成无前提保证。")
        if re.search(r"库存|扣减|超卖", subject):
            checks.append("跨库时说明用户能看到的中间态，例如订单待库存确认；库存服务在自己的事务内以条件更新防超卖。库存不足且未扣减无需补偿；超时未知先核对，确认扣减后取消才幂等释放。")
    if re.search(r"租约|fencing|旧线程|锁.*(?:过期|到期)", question, re.I):
        checks.append("解锁校验只防误删锁；写前检查归属与下游写之间仍有竞态，防旧写需要下游原子校验有效令牌或版本。")
    if re.search(r"支付|付款|外部.*(?:接口|副作用)", subject):
        checks.append("本地事务覆盖不到外部副作用；超时属于结果未知。一次支付意图的身份需由服务端订单映射或客户端持久化并在刷新后恢复，先按该身份查询。分别核对本服务和支付渠道的幂等作用域、结果查询能力及终态失败契约，再决定同键重试或确认未扣款后创建新意图；不能把某一种键策略写成通用规则。")
    if re.search(r"显著|新版|旧版|百分点|统计", question):
        checks.append("先逐项对应对象和数字，样本差异方向与统计显著性分开；同批样本保留配对关系，不自行补数据。")
    if re.search(r"await|协程|丢失更新", question, re.I):
        checks.append("逐步跟踪各任务的读值和写值，注明最后覆盖哪次更新；同进程锁覆盖完整读改写，不能替代跨进程数据库约束。")
    if _TCP.search(subject):
        checks.append("正常握手按消息说明谁知道什么：客户端收到有效SYN+ACK时，已经知道自己的SYN被服务端确认，也已收到服务端的SYN。此刻缺的是服务端对客户端是否收到服务端SYN的确认。最终ACK(y+1)补足服务端所缺的确认。旧重复SYN另走RST拒绝路径。")
        checks.append("回答为什么需要第三次时，直接说服务端需要收到对其初始序列号的确认。避免用自身收发能力概括各方状态。")
    if _READ_COMMITTED.search(subject):
        checks.append("PostgreSQL READ COMMITTED 的普通SELECT按语句取快照。后一次SELECT可能看到外部在两句之间提交的变化，也能看到本事务前面语句的未提交写入。")
    if _CACHE_REFILL.search(subject):
        checks.append("先提交后删缓存也可能遇到读旧值后迟到回填。若用版本校验防旧写，删除后仍需保有可比较的最新版本或墓碑，并原子地校验与回填；只对空key做CAS仍可能接受旧值。")
    checks.extend(guidance for pattern, guidance in _ENGINEERING_CHECKS if re.search(pattern, subject, re.I))
    numbers = [{"literal": match[0], "context": question[max(0, match.start()-14):match.end()+14]}
               for match in re.finditer(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|条|个|秒|毫秒|年|月|件|次|周)?", question)]
    negatives = [clause.strip() for clause in re.split(r"[，,。；;？?\n]", question)
                 if re.search(r"不|未|没有|禁止|无须|无需", clause)]
    contract = {"number_object_pairs": numbers, "paired_error_counts": paired_error_counts(question), "negation_constraints": negatives,
                "engineering_checks": checks, "short_answer": "结论、一个具体机制或动作、一个关键失败边界；只展开本题需要的部分。"}
    if re.search(r"游标分页|keyset|(?:created_at|时间戳).*(?:游标|分页)", subject, re.I):
        contract["pagination_contract"] = {
            "tuple_comparison": "比较完整排序元组；并列字段也按同一方向比较",
            "next_page": {
                "ascending": "ORDER BY key ASC => WHERE key > cursor",
                "descending": "ORDER BY key DESC => WHERE key < cursor",
            },
            "membership_test": "把记录的完整键代入 WHERE；谓词为真才位于尚未扫描区间",
            "fixed_set_limit": "游标只给出位置和全序，不是可见性快照",
            "visibility_options": [
                "数据库一致性快照或可延续的快照标识",
                "分页开始时物化记录身份集合",
                "按提交/可见性版本水位过滤后来写入的行",
            ],
            "business_time_warning": "created_at <= 分页开始时间仍会接纳分页开始后补写的历史 created_at",
        }
    if re.search(r"(?:滚动发布|灰度|迁移|回滚).*(?:字段|列|schema|数据库)|(?:字段|列).*(?:删除|删掉|改名|回滚|滚动发布)", subject, re.I):
        contract["schema_rollback_contract"] = {
            "application_precondition": "目标版本访问的全部字段必须先存在且语义兼容",
            "data_precondition": "从权威值或日志恢复旧表示，并校验不可逆转换和迁移期间写入",
            "worker_precondition": "隔离或升级仍按旧协议并发写入的 worker，明确迁移期读写权威",
            "unsafe_order": "先回滚任何访问已删除旧列的版本，再恢复列",
            "safe_order": "恢复兼容 schema -> 恢复/校验数据 -> 验证读写一致性 -> 切换应用",
        }
    if re.search(r"支付|付款", subject):
        contract["payment_retry_contract"] = {
            "identity": "同一支付意图在页面刷新后仍可由订单映射或持久化标识恢复",
            "unknown_result": "504/超时先按支付意图查询，不能当作失败或直接创建第二笔支付",
            "idempotency_scopes": ["本地支付服务的原子去重", "支付渠道的幂等键或商户请求号"],
            "terminal_failure": "同键能否重试、何时必须新建意图和新键由双方合约决定；先确认没有成功扣款",
        }
    if _TCP.search(subject):
        contract['protocol_trace'] = [
            {'message':'C -> S: SYN(seq=x)', 'server_knows':'收到客户端的SYN'},
            {'message':'S -> C: SYN(seq=y), ACK(x+1)', 'server_state':'SYN-RECEIVED',
             'client_knows':'服务端确认了x，客户端收到y；服务端尚不知道客户端是否收到y'},
            {'message':'C -> S: ACK(y+1)', 'server_knows':'收到对y的有效确认后进入ESTABLISHED'},
        ]
    if re.search(r'await|协程|丢失更新', question, re.I):
        contract['interleaving_example'] = {'kind':'hypothetical_not_question_fact', 'initial':100,
            'steps':['A读100后await','B读100后await','A写110','B基于旧快照写110'],
            'expected':120,'actual':110,'overwritten_update':'A的加10被B的旧快照写覆盖',
            'lock_boundary':'若请求必须移到锁外，须在锁内重新读取当前值或核对版本后计算，不能继续提交旧快照。'}
    return contract
