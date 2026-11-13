一、设计目标
1. 第一层：意图识别
   - 打招呼 / 谢谢：直接返回固定话术，不调用模型，0 Token 消耗。
   - 转人工：直接流转人工客服通道。
   - 其余产品咨询、售后类问题：进入知识库检索节点。
2. 第二层：检索结果判断
   - 命中（相似度达到阈值）：走 RAG 链路，携带检索上下文，严格按 RAG 提示词基于知识库回答。
   - 未命中（无结果 / 低于阈值）：走分级常识兜底链路。
3. 兜底规则
   - 3C 行业通用常识：可以基于行业常识回答，并标注【该内容为 3C 行业通用常识参考，非本店官方规则】。
   - 本店专属规则：禁止脑补编造，礼貌引导用户更换提问方式或转接人工客服。
   - 话术自然多变，不要每次重复相同句子，简洁友好。

二、现状与目标差距
1. 意图识别：目前 app/ai/intent.py 只有 chat/danger/llm 三类整句正则；转人工已用 match_handoff() 在 chat_stream 前置拦截，但还没有并入统一六类意图，「谢谢你」仍识别不到，业务问题也未区分规则类和数据类。
2. 打招呼/谢谢：chat 命中返回固定话术且不调模型，确实 0 Token；但只有一条话术，反复问候时完全一样，覆盖词也偏少。
3. 转人工：后端识别后只下发 handoff_offer 卡片，用户点击「联系人工客服」才置 human_mode，商城与小程序同款；后台轮询已落地，剩余问题是仅 5 秒轮询、无全局红点/声音提醒，且意图尚未并入统一节点。
4. 知识库检索：知识库只是 9 个工具之一，由模型决定是否调用；政策/店铺介绍靠 _is_kb_direct + _forced_tool 强行兜一次，不是必经节点。
5. 检索命中：knowledge_answer() 有结果时直接返回原文、不经过模型，否则把工具结果作为 ToolMessage 交给同一个模型；没有严格 RAG 提示词，也没有按相似度分档。
6. 检索未命中：search_knowledge 返回 {"matched": false}，模型按 SUMMARY_RULES 自由回答；未区分 3C 常识和本店规则，没有强制免责声明，可能编造店铺政策。
7. 相似度阈值：matcher.py 分值为完全一致 100、包含 92、关键词 55/65、Dice×60、同分类 +8，阈值 55；_match_entry 只返回 entry、丢掉 score，55 分以上的低置信结果也会被当作直接答案返回。
8. 话术多变：闲聊固定话术 1 条、兜底话术 1 条，需要多变体并按消息稳定选择。
结论：当前实现是「单模型 + 工具循环」，文档要求「意图分流 + 检索为中心 + 分级兜底」；不需要推翻工具链路——订单、售后、库存、优惠活动必须查实时接口，知识库替代不了，要做的是把规则类问题从工具循环里拆出来，做成显式节点。

三、目标链路
用户消息
  ↓
前置分支（保留）：待确认动作（确认/取消）、优惠券/活动（promo_answer 直答，优先于知识库）
  ↓
intent 节点（纯规则，0 Token）
  - chat（打招呼/谢谢/再见）→ 固定话术（多变体）
  - handoff（转人工）→ handoff_offer 卡片 + 固定话术，点击卡片后才切人工
  - danger（后台危险写操作）→ decide ⇄ tools
  - biz_kb（政策/店铺介绍）→ kb_retrieve
  - biz_tool（订单/售后/库存/商品/优惠）→ decide ⇄ tools → summary
  - llm（其他）→ decide
  ↓
kb_retrieve（top1 + score）
  - score ≥ 92：直接返回标准答案，0 Token
  - 55 ≤ score < 92：RAG（top3 上下文 + RAG_RULES）
  - score < 55：fallback_classify
  ↓
fallback_classify
  - 本店专属规则：安全话术 + 建议转人工，0 Token
  - 3C 行业通用常识：常识模型回答，代码追加免责声明

有意偏差：文档写「其余产品咨询、售后类问题都进知识库」，但本项目有真实的订单、售后单、库存接口；建议拆成规则类（政策/时效/运费/发票/店铺介绍 → kb_retrieve）和数据类（我的订单/售后进度/库存/商品价格/优惠券 → decide ⇄ tools），否则「我的订单到哪了」会被强行拉去查知识库。

四、改造清单

1. 意图层扩展（app/ai/intent.py；handoff 已完成，其余待做）
   把 agent.py 里的 _POLICY_KEYWORDS、_STORE_INTRO、_PERSONAL_ORDER、_PROMO_QUERY 迁移进来，统一输出 chat/handoff/danger/biz_kb/biz_tool/llm 六类。
   chat：问候/感谢/告别/收到，正则追加谢谢你、多谢、感谢、好的、好、收到、ok、嗯、拜拜；护栏是长度 ≤ 12 字且不含业务词，避免「好的，帮我查订单」被误判。
   handoff：转人工、人工客服、真人客服、找客服、人工服务；投诉类不直接转人工，先走知识库/大模型（已确认）。
   biz_kb：命中政策词/店铺介绍词，且不命中 _PERSONAL_ORDER 和优惠活动词；biz_tool：命中 _PERSONAL_ORDER、售后单、库存、商品/推荐、_PROMO_QUERY；danger 保持现有规则，其余归 llm。
   固定话术做多变体，用 zlib.crc32(text.encode()) % n 选择，保证同一句话稳定、不同问法有变化；问候 3 条、感谢 2 条、告别 1 条、转人工 2 条。

2. 知识库节点与三档阈值（app/ai/tools.py、app/kb/matcher.py）
   MatchResult（entry、score）已存在，但 _match_entry() 仍只返回 entry、丢掉 score；改为返回 MatchResult 并补 category，search_knowledge 返回值补 score/category，knowledge_answer 返回 (entry, score)。
   新增 match_top_k(k=3)，按分类去重，供 RAG 使用；确认语义分与词面分同为 0~100，否则会破坏分档。
   分档：≥92 完全一致/包含标准问题，直接返回原文，0 Token，避免模型改错政策；55~91 关键词/Dice/语义命中，带 top3 上下文走 RAG，由模型组织语言但必须限定在上下文内；<55 视为未命中，进入分级兜底。
   上线顺序：先透出 score 并记录日志，观察一周分布后再启用 92/55 分档。

3. 分级兜底（新增 app/ai/fallback.py 或放在 intent.py）
   先分类再决定是否调模型；本店专属规则词包括保修、质保、保价、发货时间、多久发货、几天到、运费、包邮、退换、退货、换货、退款、发票、开票、套餐、赠品、赠、本店价格、活动、优惠券、满减、会员、积分、售后流程、上门、安装、以旧换新、分期。
   3C 通用常识词包括 PD、QC、快充、氮化镓、OLED、LCD、AMOLED、刷新率、触控采样、骁龙、天玑、内存、闪存、UFS、LPDDR、WiFi、蓝牙、NFC、IP68、防水、降噪、频响、阻抗、像素、光圈、传感器、ISO；同时命中时以本店专属优先。
   本店专属直接返回安全话术、不调模型，准备 3 条变体，例如「这个属于店铺的售后/活动规则，我这边没有查到明确说明，建议转人工客服确认，避免给你错误信息」「这类本店政策我不能凭空回答，你可以换个说法，或点转人工让同事帮你核实」。
   行业常识走独立提示词生成，免责声明由代码在回答末尾固定追加，不依赖模型自觉：【该内容为 3C 行业通用常识参考，非本店官方规则】。

4. 提示词拆分（app/ai/prompts.py）
   新增 RAG_RULES：只能依据知识库上下文回答；上下文没有的政策、价格、时效一律不补充；信息不足时明确说未查到并引导用户；不要逐字复述；口语化、简洁。
   新增 COMMON_RULES：只回答 3C 通用概念；禁止涉及本店政策、价格、活动、服务承诺；不要自己写免责声明，系统会追加。
   新增 STORE_SPECIFIC_REPLIES：本店规则的安全话术变体。
   同时从 TOOL_RULES 里删掉与 RAG_RULES 重复的知识库条目约束，只保留「模型主动调 search_knowledge 时」的少量说明。

5. 图结构改造（app/ai/agent.py）
   新增 kb_retrieve、rag_answer、fallback 三个节点；路由为 chat/handoff → END、biz_kb → kb_retrieve、biz_tool/danger/llm → decide。
   kb_retrieve 分三路：direct → END、rag → rag_answer → END、miss → fallback → END（专属规则直接 END，常识回答流式输出）。
   完成后删除 _is_kb_direct 和 _forced_tool，它们的作用被 biz_kb 路由和 kb_retrieve 节点替代；保留 _PROMO_QUERY 直答，实时配置优先于知识库。
   tools 节点里 search_knowledge 强制使用用户原话的逻辑保留；汇总阶段 SUMMARY_RULES 不变，RAG/兜底节点独立流式输出，复用 _mask_ai、落库、done 逻辑。

6. SSE 事件契约（handoff 已落地，卡片式）
   现有事件：meta、token、products、confirm、done、error；转人工新增 {"type":"handoff_offer","conversationId":123,"text":"可以的，点击下方「联系人工客服」即可接入…"}，此时只落库用户消息、不置 human_mode。
   用户点击卡片调 POST /api/mall/ai/handoff，后端置 human_mode 并写入「已为您转接人工客服」提示；商城每 3 秒、后台每 5 秒轮询。小程序同样只挂卡片，点击后进入人工轮询。
   route 事件建议只在开发/后台下发，不暴露给普通用户；小程序继续复用 hide_ai 逻辑，覆盖新增节点输出和固定话术。

7. 可观测性
   日志至少记录 conversation_id、intent、route、kb_entry_id、kb_score、fallback_type、latency_ms、token 用量。
   用途：统计命中率、兜底率、转人工率；用真实 badcase 调整 55/92 阈值；验证 0 Token 链路（chat、handoff、专属规则兜底、≥92 直答）是否真的没调模型。

8. 测试
   新增 tests/test_intent.py（六类意图 + 边界，如「好的，帮我查订单」不能判 chat）、tests/test_kb_route.py（三档分数 → direct/rag/fallback）、tests/test_fallback.py（本店专属优先、免责声明固定追加）。
   小程序渠道：任何节点输出都不含「AI」。

五、验收用例
1. 你好 → chat → 固定话术，不调模型。
2. 谢谢你 → chat → 固定话术。
3. 转人工 → 先出 handoff_offer 卡片；点击卡片 → 置人工并提示已接入，商城/小程序进入人工轮询，后台可见会话。
4. 多久发货 → biz_kb 命中 → ≥92 直答或 RAG 基于上下文。
5. 什么是 PD 快充 → biz_kb 未命中 → 常识 → 回答 + 固定免责声明。
6. 你们保修几年 → biz_kb 未命中 → 专属 → 安全话术 + 建议转人工，不编造。
7. 我的订单发货了吗 → biz_tool → query_order。
8. 推荐几款手机 → biz_tool → search_products/recommend_product。
9. 有什么优惠券 → promo 直答 → 实时优惠配置。
10. 帮我写一首诗 → llm → 按现有规则礼貌拒绝。
说明：转人工卡片链路（后端 handoff_offer + 商城/小程序卡片 + 点击切人工）已实现，并通过 py_compile 与商城前端构建验证；其余用例待改造后逐条验收。

六、已确认决策
1. 投诉类先走知识库/大模型，只有用户明确说「转人工」才切人工通道。
2. 知识库先透出 score 并记录日志，观察一周分布后再启用 92/55 分档。
3. 检索未命中时允许回答 3C 行业通用常识，并由代码强制追加免责声明。
4. 后台提醒保持 5 秒轮询，暂不做实时红点/声音。
5. 本店专属规则的安全话术只给文字引导，不放「转人工」按钮；用户回复「转人工」由后端 match_handoff 接管。
6. 旧接口 /api/agent/chat 立即下线：已删除 app/api/agent.py 与 app/kb/service.py，并移除 main.py 里的路由注册。
7. 投诉安抚话术：知识库新增 46 条，分类「投诉与安抚」，话术结构为道歉→处理动作→询问是否转人工；投诉类命中后直答、不调模型，已同步到线上库（ai_kb_qa 共 805 条）。
8. 人工客服权限已核实：线上库 human_mode/human_at 字段、菜单 1007、admin/operator/service 三角色授权齐全，无需再确认。
9. 人工相关一律不调模型：match_handoff 支持裸词「人工/客服/真人」，人工信息类问题（时间、联系方式、收费、是否真人）走知识库直答；知识库补 2 条、修 2 条关键词，线上已同步；提示词禁止描述按钮位置。

七、风险与实施顺序
1. 误路由：chat 必须加长度和业务词护栏；投诉先走知识库/大模型，只有明确说转人工才切（已确认）。
2. 阈值误判：55 分附近容易把弱相关内容当成命中；先只上日志观察一周分布，再启用 92/55 分档（已确认）。
3. 直答时效性：≥92 直答完全信任知识库，后台已有 KbManage 可增删改、批量导入，但依赖运营及时维护；本店活动、价格仍应走 promo_answer 实时接口。
4. 提示词冲突：RAG/常识/工具三套规则按路由单独注入，不要叠加；TOOL_RULES 需同步精简。
5. 前端兼容：handoff_offer 已接入；商城和小程序均按事件类型分发、会忽略未知事件，新增 route 事件不会报错。
6. 旧接口：/api/agent/chat 与 /chat/stream 已下线，网关 /api/agent/** 会返回 404；未升级的旧版小程序将失去知识库问答（已确认接受）。
   新增能力一律走 /api/mall/ai/** 与 /api/admin/ai/**，不要恢复旧路由。
实施顺序：转人工（已完成）→ 旧接口下线（已完成）→ 意图扩展 → 知识库分数透出与日志（观察一周）→ 启用 92/55 分档 → 兜底分类 → 提示词 → 图改造 → route 事件 → 测试；每一步都可单独上线。
