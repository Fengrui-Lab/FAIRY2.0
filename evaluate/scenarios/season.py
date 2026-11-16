"""Full-season scenario composition: crop parameters, spatial modifiers and resource limits."""
from dataclasses import dataclass, fields
from fairy.acps.ontology.farm.spec import finite


@dataclass(frozen=True)
class SeasonSpec:
    # Explicit scenario input; empty means homogeneous FarmSpec soil.
    hydraulic_zones: tuple[tuple[int, int, float, float, float], ...] = ()
    cultivar: str = "HEINONG58"
    ridge_count: int = 64
    seed_spacing_cm: float = 7.9
    sowing_depth_cm: float = 4.0
    emergence_gdd: float = 90.0
    maturity_gdd: float = 1120.0
    max_lai: float = 4.95
    rue_g_mj_par: float = 2.35
    grain_partition: float = 0.44
    # Fraction of gross grain (both dry matter and water) recovered by machinery.
    # An explicit engineering assumption, not a calibrated biological parameter.
    harvest_recovery_fraction: float = 0.96
    initial_seed_count: int = 1000000
    initial_fuel_l: float = 190.0
    combine_capacity_kg: float = 1800.0
    trailer_capacity_kg: float = 7000.0
    storage_capacity_kg: float = 20000.0
    safe_moisture: float = 0.135

    def __post_init__(self):
        if self.cultivar != "HEINONG58":
            raise ValueError("Full-season Native currently supports the HEINONG58 proxy only")
        if type(self.ridge_count) is not int or self.ridge_count != 64:
            raise ValueError("The current spatial fixture has exactly 64 ridges")
        if type(self.initial_seed_count) is not int:
            raise ValueError("Seed inventory must be integral")
        for f in fields(self):
            if f.name not in ("cultivar", "hydraulic_zones"):
                finite(getattr(self, f.name), f.name, 0)
        for name in ("seed_spacing_cm", "sowing_depth_cm", "emergence_gdd", "max_lai",
                     "rue_g_mj_par", "combine_capacity_kg", "trailer_capacity_kg", "storage_capacity_kg"):
            finite(getattr(self, name), name, 0.001)
        finite(self.maturity_gdd, "maturity_gdd", self.emergence_gdd + 1)
        finite(self.grain_partition, "grain_partition", 0.01, 1)
        finite(self.harvest_recovery_fraction, "harvest_recovery_fraction", 0, 1)
        if self.harvest_recovery_fraction == 0:
            raise ValueError("Harvest recovery must be positive")
        finite(self.seed_spacing_cm, "seed_spacing_cm", 1, 50)
        finite(self.sowing_depth_cm, "sowing_depth_cm", 1, 10)
        finite(self.safe_moisture, "safe_moisture", 0.10, 0.135)
        covered=set()
        if not isinstance(self.hydraulic_zones,tuple): raise ValueError("Zones must be immutable")
        for zone in self.hydraulic_zones:
            if not isinstance(zone,tuple) or len(zone)!=5: raise ValueError("Invalid hydraulic zone")
            first,last,capacity,capture,efficiency=zone
            if type(first) is not int or type(last) is not int or not 0<=first<=last<self.ridge_count:
                raise ValueError("Invalid hydraulic range")
            if covered.intersection(range(first,last+1)): raise ValueError("Overlapping hydraulic zones")
            covered.update(range(first,last+1))
            finite(capacity,"field capacity",0.01,.99)
            finite(capture,"rain capture",0,1)
            finite(efficiency,"irrigation efficiency",.01,1)
