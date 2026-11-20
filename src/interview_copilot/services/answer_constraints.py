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


def reasoning_contract(question):
    checks = []
    if re.search(r"outbox|发消息|消息投递|可靠投递", question, re.I):
        checks.append("若采用Outbox：发送成功、标记完成前崩溃仍会重发；说明至少一次与消费者幂等，不承诺绝不重发。")
        if re.search(r"库存|扣减|超卖", question):
            checks.append("先按状态分开：库存不足且未扣减时结束失败、无需回补；暂时不可用可重试，超时未知先查结果；确认已扣减后又取消，才以幂等补偿释放库存。")
    if re.search(r"租约|fencing|旧线程|锁.*(?:过期|到期)", question, re.I):
        checks.append("解锁校验只防误删锁；写前检查归属与下游写之间仍有竞态，防旧写需要下游原子校验有效令牌或版本。")
    if re.search(r"支付|付款|外部.*(?:接口|副作用)", question):
        checks.append("本地事务覆盖不到外部副作用；超时属于结果未知，先核对幂等与结果查询能力，再决定恢复路径。")
    if re.search(r"显著|新版|旧版|百分点|统计", question):
        checks.append("先逐项对应对象和数字，样本差异方向与统计显著性分开；同批样本保留配对关系，不自行补数据。")
    if re.search(r"await|协程|丢失更新", question, re.I):
        checks.append("逐步跟踪各任务的读值和写值，注明最后覆盖哪次更新；同进程锁覆盖完整读改写，不能替代跨进程数据库约束。")
    if re.search(r"TCP|三次握手|两次握手", question, re.I):
        checks.append("按SYN、SYN+ACK、ACK核对双方状态与序列号确认；最终ACK确认服务端SYN。服务端发完SYN+ACK时尚不能确认客户端已收到，不能把ACK描述成拒绝连接的RST。")
    numbers = [{"literal": match[0], "context": question[max(0, match.start()-14):match.end()+14]}
               for match in re.finditer(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|条|个|秒|毫秒|年|月|件|次|周)?", question)]
    negatives = [clause.strip() for clause in re.split(r"[，,。；;？?\n]", question)
                 if re.search(r"不|未|没有|禁止|无须|无需", clause)]
    contract = {"number_object_pairs": numbers, "paired_error_counts": paired_error_counts(question), "negation_constraints": negatives,
                "engineering_checks": checks, "short_answer": "结论、一个具体机制或动作、一个关键失败边界；只展开本题需要的部分。"}
    if re.search(r'TCP|三次握手|两次握手', question, re.I):
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
