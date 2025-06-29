// &>/dev/null || /usr/bin/env go run "$0" "$@"; exit $?

package main

import (
    "fmt"
    "os"
)

func main() {
    if len(os.Args) < 2 {
        fmt.Println("Usage: ./myscript.go [your_name]")
        return
    }
    name := os.Args[1]
    fmt.Printf("Hello, %s! This Go script is running like a charm.\n", name)
}


