# L3 full-season scenarios

Declarative task inputs only; no engine construction or legacy App registration.
`soybean_full_season.py` reconstructs the selected 64-ridge, HEINONG58, 4 mm quota
scenario from an unplanted field to safe storage. No detail/library context or
Oracle workflow is embedded in the task prompt. `weather.json` is fixed synthetic
forcing with source/hash in the Native process audit. Public forecast access is
limited to the declared weather service; full future forcing is not in context.

This is a new reduced process implementation, not numerical or agronomic
equivalence to the historical scenario. Nutrient and biotic management are not
implemented. The same CLI and Runtime used by L1 execute this scenario.
