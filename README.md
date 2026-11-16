# 专项资金分摊与舍入尾差复核

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 预算采信：`/api/budgets/import`
- M2 资格校验：`/api/eligibility/evaluate`
- M3 权重测算：`/api/weights/calculate`
- M4 金额分摊：`/api/allocations/calculate`
- M5 尾差裁决：`/api/remainders/resolve`
- M6 批次复核：`/api/export/allocations.csv`
- M7 公示与封存：`/api/seals` `/api/disclosures` `/api/seals/{batch_id}/original`

        ## 核心规则

        先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18213`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。

        ## 公示与封存（M7）

        - 封存：`POST /api/seals`，原样保存资格材料与既有判定，不重算资格。
        - 公示：`POST /api/disclosures`，只保留项目与获资金额；下载入口 `/api/disclosures/{disclosure_id}/download.csv`。
        - 撤回：`POST /api/disclosures/{disclosure_id}/retract`，旧下载入口立即失效；公开总额、批次与封存状态仍可回读。
        - 审计查阅：`GET|POST /api/seals/{batch_id}/original`，需审计角色（`x-role: auditor`）、操作人与理由；留痕失败即拒绝返回原文。
