# Oracle training and qualification

`environment.py` wraps the existing Native full-season runtime as a Gymnasium environment. Fixed preparation and harvest/storage drivers isolate dose learning; they are not a generated LLM workflow. All work uses public operations. Separate bounded normal training variants have explicit different weather and initial soil inputs, rather than pretending a changed random seed changes fixed weather. The registered HEINONG58 normal case is not mutated or used to tune reward.

`qualification.py` independently reexecutes final operation logs and checks the existing task evaluator. It reports task validity, clean execution, numerical usage and trained-policy provenance separately. Candidate selection is best-known among explicitly listed qualified attempts, under the predeclared actual-storage/water objective; no global optimum is claimed.

The existing evaluator is unchanged. Historical maize/WOFOST normal proposals are not local capabilities: this first implementation uses the rebuilt HEINONG58/Native normal. Since 0.8.0, explicit Soybean_901 proxy WOFOST full-season scenarios and backend-scoped training are supported; HEINONG58 Native cases still reject WOFOST rather than silently substituting parameters. See [current validation](../../demo/WOFOST_FULL_SEASON.md).

Reference selection includes valid non-learning controls rather than filtering out a better baseline. Trained-stack integration and best-known workflow quality have separate qualification fields. The numerical schedule counts distinct daily model decisions; cached/revalidated advice certificates must not trigger a second inference or second irrigation in the same window.

The [historical PPO representation study](PPO_REPRESENTATION.md) uses a separate 60 mm full-field experiment with archived forcing, matched public features, timing controls, and a validation-before-holdout protocol.

The [numerical dose workflow](NUMERIC_DOSE.md) defines the current objective and its execution split between supervised diagnosis, LLM operation selection, and a conditional dose policy.
