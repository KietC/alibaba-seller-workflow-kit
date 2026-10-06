# Live Alibaba.com Customer CRM EDM readback / 实时页面回读

Read the current signed-in MyAlibaba Customer CRM page before preparing any local draft. Record only non-sensitive UI labels, channel readiness, quota availability, and field constraints in a private local run receipt. A historical observation never proves current availability.

准备本地草稿前先读取当前已登录的 MyAlibaba 客户通页面。仅在私有本地回执中记录不敏感的界面标签、渠道状态、额度是否可用及字段限制。历史观察不能证明当前可用。

If the channel is exhausted, disabled, inaccessible, or uncertain, stop at LOCAL_DRAFT_ONLY and do not fill, activate, schedule, test-send, or send. Standalone OKKI CRM and Alibaba.com Customer CRM are separate contexts; do not reuse accounts, audiences, or captured state across them.

渠道耗尽、未启用、不可访问或不确定时，停在 LOCAL_DRAFT_ONLY，不填入、不启用、不排期、不试发、不发送。独立 OKKI CRM 与 Alibaba.com 客户通是不同业务环境，禁止混用账号、客群或已捕获状态。
