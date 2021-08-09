/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package net;

import java.io.DataInputStream;
import java.io.IOException;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketAddress;
import java.util.LinkedList;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import ses.EggyRush;

/**
 *
 * @author Creativ
 */
public class Listener extends Thread{

    private List<SocketAddress> SAList;
    private ServerSocket sk;
    private List<Peer> PeerList;
    
    public Listener() {
        try {
            this.PeerList = new LinkedList<>();
            sk = new ServerSocket();
            
        } catch (IOException ex) {
            Logger.getLogger(Listener.class.getName()).log(Level.SEVERE, null, ex);
        }
    }

    public void reference(List<SocketAddress> SAList){
        this.SAList = SAList;
        try {
            sk.bind(SAList.get(EggyRush.myID));
        } catch (IOException ex) {
            Logger.getLogger(Listener.class.getName()).log(Level.SEVERE, null, ex);
        }
    }
    @Override
    public void run() {
        for(int i=0; i<EggyRush.myID; i++){
            try {
                
                Socket newSocket = sk.accept();
                DataInputStream dataInputStream = new DataInputStream(newSocket.getInputStream());
                int PeerID = dataInputStream.readInt();
                this.PeerList.add(new Peer(PeerID, newSocket));
                System.out.println("P" + PeerID + " accepted");
                
            } catch (IOException ex) {
               System.out.println(ex.getMessage());
            }
        }
    
    }
    
    public List<Peer> getGuestList(){
        return this.PeerList;
    }
    
}
