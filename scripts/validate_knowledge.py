"""Compare lexical/context-only answers with grounded hybrid answers on local materials.

Outputs remain under ignored data/. Uses the configured answer provider and cached
local embeddings. Does not start audio, write interview history, or alter reviews.
"""

import argparse
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT / "src")]

from copilot.llm import LLMClient, _load_env_files
from interview_copilot.services.answer_content import parse_answer
from interview_copilot.services.answer_context import _evidence_within
from interview_copilot.services.corpus import CorpusIndex
from interview_copilot.services.documents import DocumentLibrary
from interview_copilot.services.knowledge import KnowledgeStore, locate_quote
from interview_copilot.services.provider_settings import ProviderSettings
from interview_copilot.services.semantic import SemanticIndex


QUESTIONS = [
    "请自我介绍，重点讲知识图谱与关系抽取方面的经历。",
    "肝病图谱如何处理同一个实体在不同数据库中名字不一致的问题？",
    "MirrorRE 的双向镜像机制和 10-shot 结果是什么？",
    "GuideRE 为什么采用实体盲重写？和常规的句子级检索有什么区别？",
    "AoG 如何让 LLM 推理意图跟图结构动态对齐？",
    "我已经毕业了吗？我的资料里毕业时间是否一致？",
    "你已经独立上线支持百万并发的金融系统了吗？",
    "这份 Agent-Harness 架构指南能证明你已经独立实现并上线了这套系统吗？",
    "解释 Python GIL 对 CPU 密集型多线程任务的影响，如何利用多核？",
    "如果让你设计一个影院在线选座系统，怎样避免两个人买到同一座位？",
    "你怎么理解 TCP 三次握手？为什么不是两次？",
    "如果不用你用LongTrain 不用任何框架那你去写一些生产机的Agent Loop的尾代码，然后有哪些状态，状态怎么去转移，终止条件有哪几种，每一轮结束后上下文字有变化？",
    "Gephi-SK的高级封章，说明这个Function Call的完整消息协议流，SCHEMA怎么定义，以什么形式传给模型，模型反环什么结构，怎么路由到真实函数，结果以什么身份回传？",
    "那如果新版是2%的抽率，旧版是3.5%的抽率，这个差距怎么判断是真提升还是抽样波动？",
    "一个30步的长远路跑到第17步，正在执行一个抓取工具时用户点了停止，正在跑的工具调用怎么办，已经完成16步的状态怎么收尾？",
]

HISTORIES = {14:[{"role":"user","content":"如何评估 Prompt 改版后在财务研报观点抽取上的错误率是否降低？"},
                 {"role":"assistant","content":"在同一批固定标注文档上对比两版，统计每条文档的新旧错误结果。"}]}


class ReadOnlyWorkers:
    cancelled = threading.Event()

    def start(self,*args,**kwargs):
        raise RuntimeError("validation does not initialize documents")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",default="data/knowledge-validation-20261118.json")
    parser.add_argument("--retrieval-only",action="store_true")
    parser.add_argument("--question-index",type=int,action="append")
    args = parser.parse_args()
    _load_env_files()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    providers = ProviderSettings(ROOT / "data/provider_settings.db",config["llm"]); providers.start()
    settings,credentials = providers.snapshot()
    client = LLMClient(settings); client.set_credentials(credentials)
    index,workers = CorpusIndex(),ReadOnlyWorkers()
    library = DocumentLibrary(ROOT / "data/documents",index,workers)
    library.sync_index()
    documents = [doc for doc in library.list() if doc["status"] == "ready" and doc["enabled"] and not doc["archived"]]
    semantic = SemanticIndex(ROOT / "data/documents/vectors.db",model=config.get("knowledge",{}).get("semantic_model","BAAI/bge-m3"))
    semantic.activate("uploads:" + doc["id"] for doc in documents)
    knowledge = KnowledgeStore(library,workers,lambda *args:None,semantic=semantic)
    semantic.sync(knowledge.semantic_documents(documents),cancelled=workers.cancelled)
    checks,counts = [],{}
    for doc in documents:
        detail = knowledge.detail(doc["id"])
        text = library._text_path(doc["id"]).read_text(encoding="utf-8")
        counts[doc["name"]] = {"status":detail["status"],"facts":len(detail["facts"]),"partial":detail.get("partial"),
                               "rejected_quotes":detail.get("rejected_quotes"),"states":{}}
        for fact in detail["facts"]:
            assert locate_quote(text,fact["quote"],*fact["lines"]),"stored quote no longer matches source"
            states = counts[doc["name"]]["states"]; states[fact["state"]] = states.get(fact["state"],0) + 1
            if doc["kind"] in {"paper","reference"}:
                assert fact["base_state"] == "reference","reference promoted to personal fact"
    budget = config.get("corpus",{}).get("quick_budget_chars",2400)
    report = {"model":settings.get("quick_model"),"documents":counts,"semantic":semantic.stats(),"questions":checks}
    output = ROOT / args.output; output.parent.mkdir(parents=True,exist_ok=True)
    for number,question in enumerate(QUESTIONS,1):
        if args.question_index and number not in args.question_index:
            continue
        trial = {"question":question}; checks.append(trial)
        for mode in ("baseline","enhanced"):
            index.semantic = semantic if mode == "enhanced" else None
            if mode == "enhanced":
                knowledge.sync_fact_index()
            else:
                index.update_documents("facts",[])
            started = time.monotonic()
            evidence = index.search(question,budget=budget,limit=3)
            if mode == "enhanced":
                evidence = _evidence_within(knowledge.evidence(question,limit=2) + knowledge.annotate(evidence),budget)
            run = {"evidence":evidence,"retrieval_seconds":round(time.monotonic()-started,3)}
            if mode == "enhanced":
                run["semantic_candidates"] = semantic.search(question,limit=8)
            trial[mode] = run
            if not args.retrieval_only:
                answers,errors,packs = [],[],[]
                client.stream_quick(HISTORIES.get(number,[]) + [{"role":"user","content":question}],library.resume()[0],
                    lambda text:None,answers.append,errors.append,evidence=evidence,
                    candidate_profile=knowledge.profile() if mode == "enhanced" else "",
                    on_context=packs.append)
                run.update(answer=answers[-1] if answers else "",errors=errors,
                           sent_chars=packs[-1].sent_chars if packs else 0)
                assert not errors and answers,"answer request failed"
                parsed = parse_answer(answers[-1],"quick",packs[-1].sources)
                run["findings"] = parsed["findings"]
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"question":number,"baseline_sources":len(trial["baseline"]["evidence"]),
                          "enhanced_sources":len(trial["enhanced"]["evidence"]),
                          "semantic_seconds":trial["enhanced"]["retrieval_seconds"]},ensure_ascii=False),flush=True)
    client.close(); index.close()
    print("Validation report: " + str(output),flush=True)


if __name__ == "__main__":
    main()
