name>



# current-db ,
```
db
db.getName()
```

# list-users ,
```
db.getUsers()
```


# list-roles ,
```
db.getRoles()
```


# grant-roles , 
https://docs.mongodb.com/manual/reference/built-in-roles
```
db.grantRolesToUser(
    "username",
    [
        { role: "userAdmin" , db: "dbname" }
    ]
)
```
```
db.grantRolesToUser(
    "admin",
    [
        { role: "clusterAdmin" , db: "admin" }
    ]
)
```

# add-user ,
```
db.createUser(
    {
        user: "username",
        pwd: "123",
        roles: [
            { role: "userAdmin", db: "database_name" }
        ]
    }
)
```

```
db.createUser(
    {
        user: "xman",
        pwd: "123",
        roles: [
            { role: "userAdmin", db: "herodb" }
        ]
    }
)
```

# change-password ,
```
db.changeUserPassword("username", "123")
```

# init-replicate , 
```
rs.initiate(
    {
        _id : "test-rs",
        members: [ 
            { _id : 0, host : "192.168.13.215:27017" }, 
            { _id : 1, host : "192.168.13.218:27017" },
            { _id : 2, host : "192.168.13.219:27017" } 
        ]
    }
)
```


# remove-replica ,
```
db.system.replset.remove({});
```

# find-replica , 
```
db.system.replset.find();
```

# replication-setup , add-member-node , 
```
rs.add("192.168.13.219:27017");
```


# remove-replica-set ,
```
use local
db.system.replset.find();
db.system.replset.remove({});
```



# show-collection ,
```
show collections
```


# find-collection , 
```
db.new_collection.find()
```

# delete-database ,
```
db.dropDatabase()
```


# start-server ,
```bash
mongod --port 27017 --dbpath /var/lib/mongodb --replSet "test-rs"
```


# backup-database , dump-database , export-database , 
```bash
mongodump --out /data/backup/
```

# restore-database , 
```bash
mongorestore --host mongodb1.abc.net --port 27017 --username "user" --password 'pass' /opt/backup/mongodump-1945-01-18
```

# mongodump-convert-bson-to-json
```
ls *.bson |  xargs -I {} bsondump --pretty --bsonFile={} --outFile={}.json
```

# show-parameters ,
```
db.adminCommand( { getParameter : '*' } )
```


# export-to-csv ,
```
mongoexport  -d DB_NAME -c COLLECTION_NAME -u USER  -h HOST:27017  -p 'PASSWD' --authenticationDatabase=admin -o  OUTFILE --type=csv --fields='FIELD_A,FIELD_B,FIELD_C'
```


### Access
```bash
USERNAME=
DATABASE=
PASSWORD=
CLUSTER_SRV=

mongo "mongodb+srv://${USERNAME}:${PASSWORD}@${CLUSTER_SRV}/${DATABASE}"
```

### Export database
```bash
USERNAME=
DATABASE=
PASSWORD=
HOST1=
HOST2=
HOST3=
CLUSTER_NODES="${HOST1},${HOST2},${HOST3}:27017"

mongodump -u $USERNAME  -p $PASSWORD \
    --db $DATABASE \
    --authenticationDatabase admin \
    --ssl --sslAllowInvalidHostnames  --forceTableScan \
    --host $CLUSTER_NODES \
    --out ./
```

### Restore from files
```
USERNAME=
DATABASE=
PASSWORD=
HOST1=
HOST2=
HOST3=
CLUSTER_NODES='${HOST1},${HOST2},${HOST3}:27017'

mongorestore -u $USERNAME -p $PASSWORD \
    -d $DATABASE \
    --authenticationDatabase admin \
    --ssl --sslAllowInvalidHostnames  \
    --host $CLUSTER_NODES  ./
```
