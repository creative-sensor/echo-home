BEGIN { FS="|"; OFS=" " }
{
    # Trim whitespace from fields 2 through 5
    gsub(/^ *| *$/, "", $2);
    gsub(/^ *| *$/, "", $3);
    gsub(/^ *| *$/, "", $4);
    gsub(/^ *| *$/, "", $5);
    # Print the relevant fields separated by spaces
    print $2, $3, $4, $5
}
