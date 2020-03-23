This page present different failover scenarios over 3-node mongodb cluster across regions.

# 0 Model
./mongodb-3n-across-regions.svg

# 1 Configuration
### 1.1 Mongo client driver
```
mongodb+srv://mongo-db-cluster-012.domain.test/?${OPTION}
```

OPTIONS:
```
readPreference=nearest
readConcernLevel=majority
w=1

```
### 1.2 Mongo cluster
Using replica set

### 1.3 Global DNS Name

* Global URL (use policy record):
```
global-012.domain.test
```
* Regional URL:
```
region-0.domain.test
region-1.domain.test
region-2.domain.test
```
* Traffic Policy : Forwarding traffic based on user geolocation:
```
- Asia to region-0
- North America to region-1
- Default to region-2
```

Diagram
./route53-geolocation.svg

# 2 Failover Behavior
### 2.1 One of regions went down entirely
* New primary elected in remaining regions
* Global DNS name does healthcheck and disable traffic forwarding against failure region
* App instance (mongo client driver) select new primary (WRITE) and nearest secondary node (READ)


### 2.2 One-or-max-two peering link failure

#### 2.2.1 Link failure number = 1
* The secondary node which has link failure leave the cluster
* App instance (mongo client driver) select new primary (WRITE) and nearest secondary node (READ)

#### 2.2.2 Link failure number = 2
* Case A:
```
- The primary node that has 2 link failure leave the cluster. New primary node is elected.
- App instance (mongo client driver) select new primary (WRITE) and nearest secondary node (READ)
```
* Case B:
```
- The secondary node that has 2 link failure leave the cluster. No election is needed.
- App instance (mongo client driver) select new primary (WRITE) and nearest secondary node (READ)
```
### 2.3 The server that backs App instance crashed
* Global DNS name does healthcheck and disable traffic forwarding against region which has App instance failure
