# acadtrack handoff

## Status snapshot
The application is a reusable academic attendance and progress tracker. Privacy cleanup is in progress locally; this note contains no personal deployment or academic records.

## In progress
- Replace all historical personal documents and seed files with public-safe examples while preserving application code history.
- Next: finish offline tests and history scans, then record concrete verification evidence.

## Next steps
1. Verify synthetic timetable and semester imports against the application.
2. Run the offline test suite, historical privacy verification and secret scan.
3. Review example environment, ignored runtime paths and generic deployment scripts.

## Decisions (append-only)
- All bundled academic seed data is fictional. Reserved example addresses are placeholders.
- Credentials, databases and personal import files must remain outside version control.
- Generic deployment examples must obtain configuration from this application rather than another service.
- No live API calls are required to run the test suite.

## Key pointers
- app.py contains the web app and shared timetable importer.
- timetable.json and semester.json contain coherent synthetic demonstration data.
- examples/.env.example documents optional local configuration.
- tests/ verifies scheduling, imports, authentication guards and mocked AI interpretation.

## How to resume
Read this note, inspect Git status and run the documented offline verification before changing the application. Configure your own host, accounts and private academic data outside Git.

## Verification and current state
- Synthetic data and generic configuration examples are ready for independent review.
- Offline verification: 76 tests pass using `python -m pytest -q --disable-socket` (including the example import integration check). Network sockets were disabled throughout the run.
- The demonstration imports create two courses, two recurring slots, three topics and two example assessments. Their shared term and example break combine correctly, and reimporting does not duplicate records.
- A history privacy check against a separate private marker manifest failed on the original history and passed after local sanitization. The private manifest is deliberately kept outside this repository.
- Historical private handoff content has been replaced with this public-safe workflow note. Commit attribution uses the repository owner's public no-reply identity.

## Remaining review
- Independently review the full history and final secret-scan evidence before publishing.
- Configure actual credentials, identity allowlists, hostnames and personal academic data only in ignored local files.
- Generic deployment scripts were not run against any server. The application's live OAuth or model endpoints were not contacted during verification.
