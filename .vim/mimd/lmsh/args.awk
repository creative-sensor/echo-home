BEGIN { FS="|"; OFS=" " }
{
    # Trim whitespace from fields 2 through 4
    gsub(/^ *| *$/, "", $2);
    gsub(/^ *| *$/, "", $3);
    gsub(/^ *| *$/, "", $4);
    
    # Print the Agent Shell and the LLM Kernel separated by a space
    # This allows Vimscript to split the output into parts[0] and parts[1]
    print $2, $4
}
