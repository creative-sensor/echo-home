
console.log(document.getElementById("github"));
console.log( window.ipc.ETRON_APP);



const tabGroup = document.querySelector("tab-group");
console.log(`ETRON_APP : ${window.ipc.ETRON_APP}`);
window.ipc.shell_exec("pwd").then(  (result) => {console.log(`PWD : ${result.stdout}`)} );

document.getElementById("go_dpez").addEventListener(
    'click' ,
    () => {
        tabGroup.addTab({
            title: window.ipc.ETRON_APP,
            //src: window.ipc.ETRON_APP + "/index.html",
            src: document.getElementById("text_9v73").innerHTML,
            active: true,
            ready: function(tab) {
                tab.element.classList.add("ETRON");
            }
        });
    }
);

tabGroup.addTab({
  title: window.ipc.ETRON_APP,
  //src: window.ipc.ETRON_APP + "/index.html",
  src: "https://www.thisiscolossal.com/",
  active: false,
  ready: function(tab) {
    tab.element.classList.add("ETRON");
  }
});

