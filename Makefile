# Compiler to use (clang is standard on macOS and supports ARM64)
CC = clang

# Source file
SRC = rid_generator.c

# Target executable name
TARGET = rid_generator

# Default target: build the executable
all: $(TARGET)

# Rule to link the executable
$(TARGET): $(SRC)
	$(CC) $(SRC) -o $(TARGET)

# Clean up generated files
clean:
	rm -f $(TARGET)

# Phony targets so 'make' doesn't look for files named 'all' or 'clean'
.PHONY: all clean
