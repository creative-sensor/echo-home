# VimPage Specification

## Overview
This document outlines the general specification for the `vimpage` system, which functions as a mechanism to render Markdown files as a user interface. The system is designed to bridge the gap between structured data (Markdown files) and interactive display.

## Core Concept
`vimpage` provides a framework for interpreting Markdown content and mapping it to interactive elements, allowing users to view and interact with data presented in a structured, navigable manner. **A core principle of this design is the use of keymaps and inline context data to facilitate direct UI interaction with the underlying data.**

## Component: ptrm (Instance)
The `ptrm` component represents a specific instance or context within the `vimpage` framework. It is responsible for handling the specific logic related to port-remote operations and status checks, as demonstrated in the implementation files.

## Functionality Mapping
The system relies on mapping specific commands or context markers within the Markdown files to executable functions (e.g., `ptrm`, `ptrmo`, `ptrmc`).

### 1. Data Source
The primary data source for the UI is the Markdown file (e.g., `ptrm.md`).

### 2. Interaction Flow
When a user interacts with a specific element in the Markdown view (e.g., clicking a row or a link), the system triggers corresponding functions defined within the `vimpage` logic to perform actions, utilizing the context data provided inline.
*   **`ptrm`**: Invoke logic related to port-remote operations.
*   **`ptrmo`**: Invoke logic related to port forwarding/opening.
*   **`ptrmc`**: Invoke logic related to checking port status.

## Implementation Details (Reference)
Specific implementation details for how these interactions are handled are defined in the source files located in `src/java/entity/` and related modules.

## Goals
1.  Provide a clean, Markdown-based interface for complex data.
2.  Ensure that interactive actions map clearly to underlying system operations, leveraging inline context.
3.  Maintain separation between the presentation layer (Markdown) and the operational logic (Java/other modules).
