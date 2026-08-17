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
