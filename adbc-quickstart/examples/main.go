package main

import (
	"context"
	"fmt"
	"os"

	"github.com/apache/arrow-adbc/go/adbc"
	"github.com/apache/arrow-adbc/go/adbc/drivermgr"
)

func main() {
	lib := os.Args[1]
	var drv drivermgr.Driver
	db, err := drv.NewDatabase(map[string]string{"driver": lib})
	if err != nil { panic(err) }
	defer db.Close()
	cn, err := db.Open(context.Background())
	if err != nil { panic(err) }
	defer cn.Close()
	stmt, err := cn.NewStatement()
	if err != nil { panic(err) }
	defer stmt.Close()
	if err := stmt.SetSqlQuery("SELECT number FROM numbers(3)"); err != nil { panic(err) }
	rr, _, err := stmt.ExecuteQuery(context.Background())
	if err != nil { panic(err) }
	defer rr.Release()
	var n int64
	for rr.Next() {
		n += rr.Record().NumRows()
	}
	fmt.Printf("GO ADBC OK: %d rows\n", n)
	_ = adbc.Statement(stmt)
}
