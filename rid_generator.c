#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <time.h>

// Function to generate a random alphanumeric character
char random_char() {
    // Use time(NULL) for seeding, though for production, /dev/urandom should be used.
    // Since we are converting a shell script relying on /dev/urandom, we simulate randomness here.
    const char charset[] = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    int len = sizeof(charset) - 1;
    return charset[rand() % len];
}

// Function to generate the RID string
void generate_rid(int length, char *rid_buffer) {
    for (int i = 0; i < length; i++) {
        char c = random_char();
        // Convert to lowercase immediately as per shell script logic (tr -dc '[a-z0-9]' | fold -w $LENGTH)
        rid_buffer[i] = tolower(c);
    }
    rid_buffer[length] = '\0';
}

// Placeholder for UUID generation.
// In a real application, you would use a library like libuuid or platform-specific APIs.
void generate_uuid(char *uuid_buffer) {
    // Placeholder UUID for compilation purposes.
    strcpy(uuid_buffer, "a1b2c3d4-e5f6-7890-1234-567890abcdef");
}

int main(int argc, char *argv[]) {
    int length = 4;
    char rid[100];
    char rid_no_case[100];
    char uuid[40];

    // Seed the random number generator
    srand(time(NULL));

    // Handle input length
    if (argc > 1) {
        // Check if argument is a valid integer
        if (sscanf(argv[1], "%d", &length) == 1 && length > 0) {
            // The shell script uses $1 as length, but also checks if it's empty.
            // If argc > 1, we use the provided argument.
        } else {
            fprintf(stderr, "Warning: Invalid length provided. Using default length of 4.\n");
        }
    }
    
    // Ensure length doesn't exceed buffer size
    if (length > 99) {
        length = 99;
    }

    // Generate RID (which is already lowercase in our implementation)
    generate_rid(length, rid);
    strcpy(rid_no_case, rid); // Since generate_rid already lowercases

    // Generate UUID
    generate_uuid(uuid);

    // Output results
    printf("uuid  %s\n", uuid);
    printf("rid-%s    %s\n", rid, rid);
    printf("rid-%s_no_case    %s_no_case\n", rid, rid_no_case);

    return 0;
}
