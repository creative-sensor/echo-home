# node modules
npm install

# fix library
sed -i "54 a         global.fetch = require('node-fetch');"    node_modules/amazon-cognito-identity-js/lib/Client.js
