BEGIN { FS="|"; OFS=" " }
{
    # Trim whitespace from fields 2 through 4
    gsub(/^ *| *$/, "", $2);
    gsub(/^ *| *$/, "", $3);
    gsub(/^ *| *$/, "", $4);
    
    # Print the Agent Shell, the Command/Args, and the Port separated by a space
    # This allows Vimscript to split the output into parts[0], parts[1], and parts[2]
    print $2, $3, $4
}
