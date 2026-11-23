# Materials and inventories

Scope: material definitions, batches and located inventories, including seed, fertilizer, pesticide, fuel and water. Distinguish material type, stock instance, transfer and consumption. Electricity may require a supply/storage model rather than an ordinary material batch.

L1 has no material inventory; L3 has the ledger described below. General located-stock ontology objects and transfers beyond the selected grain flow are not yet implemented. The pilot irrigation quota is a usage limit, not a simulated water-source stock. Do not infer physical inventory from that quota or allow duplicate accounting in equipment and backend models.

## L3 material accounting

The full-season physical ledger tracks seeds, fuel, grain dry mass and grain
water separately. Stored batches retain measured moisture. Irrigation remains
a field-average usage quota; no water-source inventory is fabricated.

`grain.py` 定义带来源、干粮质量、水质量和唯一位置的粮食批次。
批次化收储场景的物理账本保存批次，按位置投影收割机、拖车和仓库的载荷。
烘干修改水量并保留批次身份，入库改变该批次的归属。

机载补给布局支持一种场景品种的整数粒种子和以升计量的燃油。
仓库余量等于剩余总量扣除各机载容器中的物资。装种、加油和退料
改变物资归属，播种、补种和田间耗油作业才减少剩余总量。
作业或转移失败沿用统一事务回滚。
