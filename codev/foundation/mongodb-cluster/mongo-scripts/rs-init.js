// replica_set is define by arguments

// BUILD CONFIG DICT
var config = {};
config._id = replica_set;
config.members = [
            { _id : 0, host : "node_1:27017" },
            { _id : 1, host : "node_2:27017" },
            { _id : 2, host : "node_3:27017" }
];

// INIT RS
rs.initiate(config);

