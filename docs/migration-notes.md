# Migration notes

## BeautifulSoup → Scrapling

The parser was migrated from BeautifulSoup to Scrapling. If a migration or engine swap changes output on a golden test, treat it as a signal to investigate the underlying HTML rather than assuming the old output was correct — fix genuine bugs surfaced by the new engine instead of reproducing them.
