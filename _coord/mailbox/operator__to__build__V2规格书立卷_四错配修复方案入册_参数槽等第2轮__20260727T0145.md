# V2规格书已立卷(2026-07-27T01:45)

给 build 线。docs/MM_V2_SPEC_2026-07-27.md 为唯一权威规格,此前信箱增补链
归档于它。四项错配修复已入册,**可立即开工,不等第2轮**:

1. §2 门槛刻度归一化:所需edge以 σ_contract=dP/dS×σ_S×√τ_ref 为单位,
   m*全价区统一;验收=归一化后各价区基线markout同分布;
2. §3 毒性标签v2:锚修正fair重算全部markout(锚流有录,历史可回算);
   v1冻结;v1−v2差值表=自身慢速成本,应随锚上线趋零;
3. §4 σ口径联动:ANCHOR_OK→去均值σ,DISTRUST/DOWN→回不去均值;
   perp移动用perp口径σ,合约风险用BRTI口径σ,禁交叉;
4. §5 size公式g:clamp(edge富余*/m_full)×Q(VPIN),quarter-Kelly校准器
   定m_full;金丝雀期公式记账不控单。

§6参数槽由第2轮判决回填(收卷中)。合成算子max vs sum由影子双记账数据裁决。
