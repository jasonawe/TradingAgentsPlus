# FinMem Deep Dive — 分层记忆 + 投资 Agent 对标分析

> 作者: Research Agent · 日期: 2026-09-10 · 供 Stage C 理财通用 Agent 产品决策参考
>
> 原始项目: pipiku915/FinMem-LLM-StockTrading (原 AI4Finance-Foundation/FinMem 已迁)
> 调研范围: 只读浅 clone + 关键 .py 阅读(不读 tests / data-pipeline)

---

## 1. 项目概览

**一句话定位**: 用 LLM Agent 做自动化股票/基金交易,通过 分层记忆 + 角色画像 模拟人类交易员的认知结构。

- **论文**: FinMem: A Performance-Enhanced LLM Trading Agent with Layered Memory and Character Design (Yu et al., 2023, arXiv:2311.13743)
- **发表**: AAAI 2024 Spring Symposium(扩展摘要) + ICLR 2024 Workshop on LLM Agents(正式接收);IJCAI 2024 FinLLM Challenge Task 3 冠军
- **仓库状态**: 957 stars / 196 forks / MIT / 最后 push 2024-08-18(已停滞 ~2 年)
- **代码体量**: puppy/ 核心 ~2.5k 行 Python;依赖 faiss + transformers + pydantic + guardrails-ai + langchain_community(仅 embedding);Python 3.10

---

## 2. 核心架构

```mermaid
flowchart TB
    subgraph Input[数据/输入]
        NEWS[每交易日新闻<br/>Alpaca/Refinitiv]
        Q[10-Q 季报]
        K[10-K 年报]
        PRICE[每交易日价格]
    end

    subgraph Agent[LLMAgent puppy/agent.py]
        STEP[step]
        REFLECT[_reflect]
        ACCESS[_update_access_counter]
        CHAR[character_string<br/>角色画像字符串]
    end

    subgraph Brain[BrainDB puppy/memorydb.py]
        direction LR
        S[short_term_memory<br/>L1 日级]
        M[mid_term_memory<br/>L2 季级]
        L[long_term_memory<br/>L3 年级]
        R[reflection_memory<br/>反思元记忆]
    end

    subgraph Store[存储层 per-symbol in-RAM]
        FAISS[faiss.IndexFlatIP<br/>1536-d cosine]
        SORT[SortedList<br/>按 compound_score]
        META[{text importance<br/>recency access_counter<br/>delta date id}]
    end

    subgraph LLM[LLM 推理层 puppy/chat.py]
        GPT[OpenAI<br/>gpt-4 3.5-turbo]
        GEM[Gemini Pro<br/>gcloud token]
        TGI[HuggingFace TGI<br/>self-host Llama2]
        GR[Guardrails AI<br/>+ Pydantic JSON schema]
    end

    NEWS --> STEP
    Q --> STEP
    K --> STEP
    PRICE --> PORT
    STEP --> M
    STEP --> L

    REFLECT --> CHAR
    CHAR -->|as query text| S
    CHAR -->|as query text| M
    CHAR -->|as query text| L
    CHAR -->|as query text| R

    S --> FAISS
    M --> FAISS
    L --> FAISS
    R --> FAISS
    S <--> SORT
    M <--> SORT
    L <--> SORT
    R <--> SORT
    SORT <--> META

    S -->|top-k text+ids| REFLECT
    M -->|top-k text+ids| REFLECT
    L -->|top-k text+ids| REFLECT
    R -->|top-k text+ids| REFLECT

    REFLECT --> GR
    GR --> GPT
    GR --> GEM
    GR --> TGI

    GPT --> STEP
    GEM --> STEP
    TGI --> STEP

    STEP --> PORT
    PORT --> FEED
    FEED --> ACCESS
    ACCESS -->|+/-1 boost importance| S
    ACCESS -->|+/-1 boost importance| M
    ACCESS -->|+/-1 boost importance| L
    ACCESS -->|+/-1 boost importance| R

    STEP -->|brain.step<br/>decay + jump| S
    STEP -->|brain.step| M
    STEP -->|brain.step| L
```

模块对应论文三大块: **Profiling** (character_string) · **Memory** (BrainDB 4 层) · **Decision-making** (LLMAgent.step)。

---

## 3. 分层记忆机制(重点)

### 3.1 四层而不是三层

源码 puppy/memorydb.py:461 BrainDB 同时持有 short_term_memory / mid_term_memory / long_term_memory / reflection_memory 四个 MemoryDB 实例。**第四层 reflection 是 LLM 自己生成的反思文本**,与其他三层并行写入、独立查询,不是后处理摘要。

### 3.2 每层存什么 + 怎么路由

| 层 | 写入函数 | 输入来源 | 衰变因子 | jump threshold |
|---|---|---|---|---|
| short (L1) | add_memory_short | agent.py:179 _handling_news 每交易日新闻 | recency=3, importance=0.92 | upper=60 升到 mid |
| mid (L2) | add_memory_mid | _handling_filings 10-Q 季报 | recency=90, importance=0.967 | lower=60 / upper=80 |
| long (L3) | add_memory_long | _handling_filings 10-K 年报 | recency=365, importance=0.988 | lower=80 降到 mid |
| reflection | add_memory_reflection | agent.py:415 LLM 每次反思产出 summary_reason | 同 long | 无阈值 |

阈值来源: config/tsla_gpt_config.toml,不同 config 可调。

### 3.3 写入与评分(puppy/memorydb.py:84 add_memory)

```python
def add_memory(self, symbol, date, text):
    # ...faiss 向量入库到 index
    importance_scores = [self.importance_score_initialization_func() for _ in text]
    recency_scores = [self.recency_score_initialization_func() for _ in text]  # 恒为 1.0
    partial_scores = [
        self.compound_score_calculation_func.recency_and_importance_score(
            recency_score=r, importance_score=i
        ) for i, r in zip(importance_scores, recency_scores)
    ]
    self.universe[symbol]["score_memory"].add({
        "text": text, "id": ids[i],
        "important_score": importance_scores[i],
        "recency_score": recency_scores[i],
        "delta": 0,
        "important_score_recency_compound_score": partial_scores[i],
        "access_counter": 0, "date": date,
    })
```

每条记忆的实际入层决定于**初始 importance_score 抽样**:

```python
# puppy/memory_functions/importance_score.py
class I_SampleInitialization_Short:
    def __call__(self): return np.random.choice([50,70,90], p=[0.5,0.45,0.05])
class I_SampleInitialization_Mid:
    def __call__(self): return np.random.choice([40,60,80], p=[0.05,0.8,0.15])
class I_SampleInitialization_Long:
    def __call__(self): return np.random.choice([40,60,80], p=[0.05,0.15,0.8])
```

> 关键设计: 即便被强行写入短期,50% 的短记忆也有 70 的初始分,会立刻越过 mid 的 jump 下限;**层级不是死的桶,是 importance score 驱动迁移**。

### 3.4 衰减与跨层跳跃

每次 brain.step():

1. **decay** (memorydb.py:248): recency_score = exp(-delta/recency_factor), importance_score *= importance_factor, delta += 1
2. **cleanup** (memorydb.py:270): recency < 0.05 or importance < 5 直接丢
3. **jump** (memorydb.py:297 prepare_jump + brain.step 内部循环 2 次):
   - importance >= upper → **跳到上一层** (accept_jump direction=up 时重置 recency=1.0, delta=0)
   - importance < lower → **跳到下一层**
   - 短→中→长 依次向上传递,长→中 向下传递

> **核心创新**: 记忆层不是写入时定终身,而是基于 importance score **双向迁移**——长期记忆被反复引用就会升级到反思层,过期信息会自动降级。

### 3.5 读: query() 双重排序

memorydb.py:138 query() 用同 query 向量做两次 FAISS 搜索:

- **P1**: top-k cosine 相似度
- **P2**: top-k 按 compound_score (min(importance,100)/100 + recency_score) 排序
- 两组合并 → merge_score = similarity + recency_and_importance → 取 top-k 去重

> 即: 既要像又要重要又要新。这套公式同时驱动**排序键**和**最终合并排序**。

### 3.6 反馈驱动的强化(credit assignment)

agent.py:545 _update_access_counter:每次 step 末,根据 Portfolio.get_feedback_response()(过去 lookback=7 天的累计收益方向 +/-1/0),对**这次决策实际引用过的 memory id**(LLM 在 summary_reason 里必须返回 id,见 prompts.py:train_prompt JSON schema)做:

```python
# memory_functions/access_counter.py
class LinearImportanceScoreChange:
    def __call__(self, access_counter, importance_score):
        return importance_score + access_counter * 5
```

赢的交易引用的记忆 → 涨 5 分 → 更容易穿越 jump threshold 升层 → **整个机制构成了一个 LLM 时代的经验萃取闭环**。

---

## 4. Agent 决策循环

入口 puppy/agent.py:565 LLMAgent.step():

```python
def step(self, market_info, run_mode):
    cur_date, cur_price, cur_filing_k, cur_filing_q, cur_news, cur_record = ...
    # 1. 写入: 季报 mid, 年报 long
    self._handling_filings(cur_date, cur_filing_q, cur_filing_k)
    # 2. 写入: 新闻 short
    self._handling_news(cur_date, cur_news)
    # 3. 股价更新到 portfolio
    self.portfolio.update_market_info(cur_price, cur_date)
    # 4. 反思: 4 层 top-k 检索 + LLM 决策
    self._reflect(cur_date, run_mode, cur_record)
    # 5. 构造动作 (train=看 record 强制 buy/sell, test=LLM 选 buy/sell/hold)
    cur_action = self._construct_train_actions(cur_record) if train else self.__process_test_action(...)
    # 6. portfolio 记账
    self._portfolio_step(cur_action)
    # 7. 用收益反馈给引用过的记忆 id 加 / 减 importance
    self._update_access_counter()
    # 8. 触发 brain.step() 做 decay + cleanup + jump
    self.brain.step()
```

**Observation → Thinking → Decision 的实现**:

- **Observation**: market_info tuple (environment.py:market_info_type) 传入 7 元组:日期/价/10-K/10-Q/新闻/未来收益/终止标记
- **Thinking**: __query_info_for_reflection 用 character_string(角色画像)作为 embedding query,各层 top_k=3 拉取 → _test_response_model_invest_info 拼成 prompt → trading_reflection 调 Guardrails
- **Decision**: 训练模式下被强制按 record 走(为了制造数据);测试模式下读 LLM 输出 investment_decision ∈ {buy, sell, hold}

**Prompt 模板** (puppy/prompts.py):

- test_prompt: Given the information... buy/sell/hold... 必须输出 4 层 memory id 以让反馈系统能定位
- train_prompt: explain why the market behaved like this... 同样要 4 层 id
- 两套都强约束 JSON 输出,Guardrails 失败 reask 1 次

**结构化输出**: puppy/reflection.py:_test_reflection_factory 用 Pydantic + guardrails.ValidChoices(id_list) 强制 LLM 只能从实际检索到的 id 里选,失败自动 reask。

---

## 5. LLM 配置

**Provider 抽象** (puppy/chat.py): ChatOpenAICompatible 通过 model 名前缀分支:

| model 名前缀 | 端点 |
|---|---|
| gpt-* | OpenAI /v1/chat/completions, Bearer api_key |
| gemini-pro | Vertex AI, gcloud 拉 access token |
| tgi | 自托管 HuggingFace TGI, LLama2 chat template |

**默认配置**(3 个 toml 任选):

- config/tsla_gpt_config.toml → gpt-3.5-turbo-0125
- config/tsla_gemini_config.toml → Gemini Pro
- config/tsla_tgi_config.toml → TGI

**Inference 层抽象**: guardrail_endpoint() 返回闭包 (input_str, **kwargs) -> str, 供 Guardrails 调用。**没有 LangChain LCEL / LangGraph**, 全部手搓 httpx + prompt 拼接 + Pydantic。Prompt 路由(safety / token 限制)直接写在 if 分支里,够糙但能跑。

**输出控制**: 强 JSON schema 输出 + ValidChoices + 1 次 reask 是唯一的一致性保障;没有 streaming、没有 function calling。

---

## 6. 记忆存储

| 维度 | 实现 |
|---|---|
| 向量库 | **FAISS in-memory** (faiss.IndexFlatIP + IndexIDMap2, cosine), 每 symbol 一个 index |
| 元数据 | sortedcontainers.SortedList 按 compound_score 排序, 每个 record 是 dict |
| Embedding | text-embedding-ada-002 via LangChain (puppy/embedding.py), **1536 维** |
| 持久化 | pickle 目录式 checkpoint (memorydb.py:380 save_checkpoint), **没有真数据库** |
| 多用户 | 通过 symbol 字典分片;**没有 user_id / session_id 维度** |
| 上下文溢出 | OpenAILongerThanContextEmb 把超长输入分块嵌入再 mean 池化 |

> ⚠️ **没有真正的向量数据库** (Chroma / pgvector / Pinecone 都没有)。生产化时这是个明显的工程债。

---

## 7. 跟我们项目的对标启示

### 7.1 跟 TradingAgents Stage C 的 L1/L2/L3 对照

| FinMem 层 | 写入来源 | TradingAgents 对标 | 适用性 |
|---|---|---|---|
| **short (L1)** | 日级新闻 | **L1 短期对话**: 当次会话 message history + 即时工具结果 | ✅ 直接对得上 |
| **mid (L2)** | 10-Q 季报 | **L2 跨会话用户偏好**: 用户风险偏好 / 资产配置 / 反复纠正 | ⚠️ **写入策略要改**: 季报频次太低, L2 需要用户关键决策/偏好变更时即时触发 |
| **long (L3)** | 10-K 年报 | **L3 长期历史引用**: 投资历史 / 成交记录 / 重大事件 | ✅ 时序对得上 |
| **reflection** | LLM 摘要 | **没有现成对应**, 可作为 agent self-improvement 摘要 新增 | 🌟 差异化亮点 |

### 7.2 可以直接借鉴

1. **Importance + Recency + Access 三因子混合评分**: 不是简单的最近最优先, 而是 similarity + recency_score + importance_score 联合召回, 公式可整体搬过来。
2. **跨层 jump 机制**: 让记忆按分数动态升降, 而不是写入时定层。L2 偏好也可老偏好如果不再被引用就降级回 L1 / 清理。
3. **Credit assignment via id 回传**: LLM 输出必须带 memory id, 事后用结果反馈(赚/亏 / 用户好评差评)反哺分数。**理财场景可以直接用用户成交后满意/不满意做反馈信号**。
4. **角色字符串作为 embedding query**: 不让 LLM 自己瞎想检索词, 而是用一句固定的 persona/expertise 当 query, 稳定且可解释。
5. **Guardrails + ValidChoices 强制结构化**: 拿 LLM 决策+引用记忆 id 是刚需, schema 强约束值得抄。

### 7.3 不能照搬

1. **FAISS in-memory + pickle**: 单进程、单用户重启即重。生产必须换 pgvector / Chroma + Redis 元数据。
2. **随机抽样初始 importance**: np.random.choice([50,70,90]) 不可解释。L1/L2/L3 应该有**显式的入层策略**(基于时间窗口 / 事件类型 / 用户显式标记)。
3. **LangChain 只用了 embedding**: 别引, 反而是负担。
4. **训练/测试双跑 + 强制 label**: 这是交易 backtest 场景特有, 我们做对话 agent 不需要 用未来收益做强制标签。
5. **没有 user/session 维度**: 当前架构按 symbol 分片, 我们要按 user_id 分片并加 ACL。

---

## 8. 借鉴清单(可落地 bullet)

- **L1 短期层**: 用 in-process vector store + 滚动窗口(最近 N 条对话), 写入即 score=高 importance, 无需跳层逻辑。
- **L2 偏好层**: 用户偏好走**显式更新事件**驱动, 不做随机抽样初始化;每次用户纠正 / 主动更新时清空旧条目或叠加 negative decay。
- **L3 历史层**: 接成交记录 / 持仓快照 / 重要生活事件(买房、换工作), 写入 importance=最高 + 极慢 decay, 基本不下沉。
- **Reflection 元记忆**: 每天 / 每周触发一次 LLM 总结, 把对话里学到的用户行为模式压成一条, 放进独立的反思层, 下次对话开始作为 system 注入。
- **三因子召回评分**: score = α·similarity + β·recency + γ·importance + δ·access_count, 权重可调, A/B 实验确定。
- **Credit assignment 闭环**: 每次会话结束 / 关键决策落地后, 根据结果(成交 / 用户反馈)对引用的 memory id 做 ±boost。
- **角色字符串驱动检索**: 在 system prompt 里维护一段我是谁的理财助手, 用这段作为所有 memory 检索的统一 query, 而不是裸 user message。
- **结构化输出 + id 回传**: 所有 LLM 决策类输出都强制带 referenced_memory_ids: List[int], 用 Pydantic + ValidChoices 校验, 失败重试 1 次。
- **跨层 jump 阈值可配置**: 借鉴 config.toml 的 [short]/[mid]/[long] 三段式, LLM Agent 启动时读, 允许产品经理改阈值 A/B。
- **Checkpoints 必须持久化到数据库**: 不用 pickle, 改 Postgres JSONB + pgvector; 每条记忆带 user_id / session_id / created_at / source_event 字段。

---

## 9. 风险 / 坑

1. **复杂度爆炸**: 4 层 + 3 因子评分 + jump + decay + cleanup + access counter, 论文里调一组超参就要重训并跑 backtest。**理财通用 agent 上线前必须先确定几层够用——大概率 3 层 + 1 个反思层足够, 不要更多**。
2. **冷启动与漂移**: importance 抽样带随机性, 长尾用户几个月后记忆库分布可能很 weird。需要监控层间记忆数量比和平均 importance 作为可观测指标。
3. **存储成本**: 每天每用户都写入所有对话 → 100 万用户 × 365 条 × 1536 维 × float32 ≈ 2.2 TB / 年向量数据。**生产必须量化压缩 / 分层索引 / 冷热分层**。
4. **feedback 噪声**: 用户事后反馈(尤其对当前持仓不满)经常迟到且有偏。Credit assignment 不能裸用 lookback-window 收益, 要加置信度 / 时间衰减。
5. **Guardrails 重依赖**: guardrails-ai 项目活跃度一般, JSON schema 校验我们直接用 instructor / pydantic + retry 即可, 别引入。
6. **LangChain 仅用于 embedding**: 这是个陷阱——一旦引入 0.x 版本会污染依赖。直接 openai.OpenAI().embeddings.create 30 行代码搞定。
7. **没有 ACL / 多租户**: 当前架构假设一个人跑一个 brain。理财场景多用户共用必须重写, 这点 FinMem 没给答案。
8. **LLM token 预算爆炸**: 每步 4 层 top-k=3 + LLM 输出 + Guardrails reask, 单步可能 5k+ tokens。每天 100 决策就是 50 万 tokens / 用户 / 天。**必须做 per-layer top_k 收敛 + 重要记忆压缩 + reflection 摘要频控**。

---

## 附录:关键文件路径(相对 FinMem 仓库根)

- README.md — 论文 + repo 结构 + 配置说明
- puppy/agent.py — LLMAgent.step 决策主循环, 657 行
- puppy/memorydb.py — MemoryDB(单层) + BrainDB(4 层编排), 828 行
- puppy/memory_functions/{importance_score,decay,recency,compound_score,access_counter}.py — 评分公式全集, ~90 行
- puppy/reflection.py — Pydantic schema + Guardrails 调用 + prompt 拼装, 455 行
- puppy/prompts.py — 53 行, 所有 prompt 字符串
- puppy/chat.py — 3 种 LLM provider 适配, 145 行
- puppy/embedding.py — OpenAI embedding + 长文本 chunking, 98 行
- puppy/environment.py — Gym-like step 环境, 131 行
- puppy/portfolio.py — 持仓 + 收益反馈, 110 行
- config/tsla_gpt_config.toml — 阈值/超参全在这, 所有层配置集中地
- figures/{memory_flow,workflow,character}.png — 论文里的官方架构图

---

## 调研元数据

- **调研耗时**: ~25 分钟(浅 clone + 13 个文件阅读 + 报告撰写)
- **可复现性**: git clone --depth 1 https://github.com/pipiku915/FinMem-LLM-StockTrading.git /tmp/finmem 即得全文
- **数据时效**: 仓库最后 push 2024-08-18, 代码再无人维护, 但论文已被多次复现, 设计仍是对标基线
