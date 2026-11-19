# Physical backends

Equipment work duration, fuel/battery consumption and material transfer belong
here. Biology owns soil and crop state; Runtime owns time, transactions and events.
`contracts.py` specifies pure plan, commit, inventory, audit and state recovery.
The initial `native/` implementation supports serial full-season jobs. L1 does
not instantiate a physical backend. No legacy App objects are used.

The Native composition currently requires the Native season biology interface;
it is not a generic adapter for PCSE. Unsupported combinations fail explicitly.
