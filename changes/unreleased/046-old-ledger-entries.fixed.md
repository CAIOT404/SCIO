The three oldest open ledger entries are closed (issue #104).
- Plan drift no longer flags a file the explorer named because the toolchain does not call it source: on T1 a tracked file the explorer named joins its step's expected files unless it is under `.scio/`, a docs file, or a dependency or lock file, so a Python product's `static/app.js` edited by a UI step is not drift (I-159).
- The dashboard's browser tests wait up to 30 s for a page (15 s for the reconnect test's socket), and a wait that times out saves a screenshot and the page's text beside the test and names them in the failure (I-170).
- Two product tests that failed once in a parallel run now print the build's halt reason when they fail; the failure was not reproduced in five parallel runs (I-145, wontfix).
