library(adbcdrivermanager)
lib <- commandArgs(trailingOnly = TRUE)[1]
drv <- adbc_driver(lib)
db <- adbc_database_init(drv)
con <- adbc_connection_init(db)
res <- read_adbc(con, "SELECT number FROM numbers(3)")
df <- as.data.frame(res)
cat("R ADBC OK: rows =", nrow(df), "\n")
