from neo4j import GraphDatabase

# Connection details - replace with your actual details
URI = "bolt://localhost:7687" # Memgraph often uses this, Neo4j uses bolt://
USER = "neo4j"
PASSWORD = "your_password"

def get_all_nodes(driver):
    query = "MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 100"
    with driver.session() as session:
        result = session.run(query)
        
        print("--- Relationships Found ---")
        for record in result:
            # Print basic information about the relationship
            print(f"Relationship: {record}")
        print("--------------------------")

if __name__ == "__main__":
    try:
        # Initialize the driver
        driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
        
        # Run the query
        get_all_nodes(driver)
        
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        # Close the driver connection
        if 'driver' in locals() and driver:
            driver.close()