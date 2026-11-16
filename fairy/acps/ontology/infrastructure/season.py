"""Irrigation, grain-facility and charging-station operation contracts."""
from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE

IRRIGATE=operation("irrigate_range","Queue water depth in mm for selected ridges; settles at midnight. Quota is field-average mm.",RANGE+(Parameter("depth_mm","number",exclusive_minimum=0,maximum=50,unit="mm"),),timing="next_local_midnight")
DRY=operation("dry_grain","Dry trailer grain to the requested wet-basis moisture fraction; removes water using electricity.",(Parameter("target_moisture","number",.10,.135),))
STORE=operation("store_grain","Transfer trailer grain into safe storage; checks moisture and warehouse capacity.")
INVENTORY=operation("inspect_inventory","Read seed, fuel, equipment battery, grain containers and stored batches.",read=True)
CHARGE=operation("charge_equipment","Charge a named electric platform (multispectral, thermal, or robot) from grid electricity.",(Parameter("equipment","string"),))
