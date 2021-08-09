/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package net;

import java.io.DataOutputStream;
import java.io.IOException;
import java.net.Socket;
import java.net.SocketAddress;
import java.net.UnknownHostException;
import java.util.LinkedList;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import ses.EggyRush;

/**
 *
 * @author Creativ
 */
public class Handshake extends Thread {

    private List<SocketAddress> SAList;
    private List<Peer> peerList;

    public Handshake() {

        this.peerList = new LinkedList<>();
    }

    public void reference(List<SocketAddress> SAList) {
        this.SAList = SAList;
    }

    @Override
    public void run() {
        try {
            Thread.sleep(9000); // wait for other processes to be up
        } catch (InterruptedException ex) {
            Logger.getLogger(Handshake.class.getName()).log(Level.SEVERE, null, ex);
        }

        for (int i = EggyRush.myID + 1; i < this.SAList.size(); i++) {
            try {
                Socket newSocket = new Socket();
                newSocket.connect(SAList.get(i));
                if (!newSocket.isConnected()) {
                    //Thread.sleep(1000);
                }
                this.peerList.add(new Peer(i, newSocket));
                DataOutputStream dataOutputStream = new DataOutputStream(newSocket.getOutputStream());
                dataOutputStream.writeInt(EggyRush.myID);
                System.out.println("Connected to P" + i);
            } catch (UnknownHostException ex) {
                Logger.getLogger(Handshake.class.getName()).log(Level.SEVERE, null, ex);
                //System.out.print(ex.getMessage());
            } catch (IOException ex) {
                Logger.getLogger(Handshake.class.getName()).log(Level.SEVERE, null, ex);
            }
        }
    }

    List<Peer> getPeerList() {
        return this.peerList;
    }
}
