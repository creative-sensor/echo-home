// db_user, db_password is defined by arguments
db = connect("localhost:27017/admin");
var USER = db_user;
var PASSWD = db_password;

var NewUser = {};
NewUser.user = USER;
NewUser.pwd = PASSWD;
NewUser.roles = [
        { "role": "userAdminAnyDatabase", "db": "admin" },
        "readWriteAnyDatabase" ,
        "clusterAdmin"
];

db.createUser(NewUser);

