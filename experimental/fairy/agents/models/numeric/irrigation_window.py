"""Shared public feature layout for the historical irrigation representation study."""

SNAPSHOT_FIELDS = (
    "unplanted",
    "pre_emergence",
    "vegetative",
    "reproductive",
    "mature",
    "harvested",
    "days_since_sowing_over_180",
    "available_water_p10",
    "available_water_p50",
    "available_water_p90",
    "low_ridge_fraction",
    "severe_ridge_fraction",
    "expert_reference_severity",
    "living_crop_mask",
    "previous_rain_over_20",
    "previous_et0_over_10",
    "weather_mask",
    "remaining_water_over_60",
    "pending_water_over_60",
)
HISTORY_DAYS = 7
HISTORY_FIELDS = (
    "day_start_mean_root_vwc",
    "day_start_severe_ridge_fraction",
    "completed_rain_over_20",
    "completed_et0_over_10",
    "settled_water_over_60",
    "day_mask",
    "weather_mask",
)
