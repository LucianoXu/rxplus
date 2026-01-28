I decided to keep API LogRecord as the first-class citizen, as the observable data in the reactive pipeline. This makes the framework more general and avoids drastic refactorization. They are emitted to the OTel SDK in LogSink.

---

No. It's better to use internal event record datatype.