const express = require('express');
const app = express();
const port = 3000;

app.get('/', (req, res) => {
  res.send('Hello from the Express demo app using a custom path!');
});

app.listen(port, () => {
  console.log(`Demo app listening at http://localhost:${port}`);
});
