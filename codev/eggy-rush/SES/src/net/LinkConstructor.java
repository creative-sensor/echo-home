/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package net;

import java.io.BufferedReader;
import java.io.FileNotFoundException;
import java.io.FileReader;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.SocketAddress;
import java.util.Collections;
import java.util.LinkedList;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import ses.EggyRush;

/**
 *
 * @author Creativ
 */
public class LinkConstructor {
    private Listener listener1;
    private Handshake handshake1;
    private PeerBook peerBook;
    
    static final String LinkFile = "LinkInfo.txt";

    private List<SocketAddress> SAList;

    public LinkConstructor() {
        readIPList();
        this.listener1 = new Listener();
        this.handshake1 = new Handshake();
        
        this.listener1.reference(this.SAList);
        this.handshake1.reference(this.SAList);
    }

    private  void readIPList() {
        SAList = new LinkedList<>();
        try {
            BufferedReader reader1 = null;
            reader1 = new BufferedReader(new FileReader(LinkFile));
            String line = reader1.readLine();
            while ((line != null) && !line.equalsIgnoreCase("")) {
                String[] fields = line.split(" ");
                InetSocketAddress socketAddress = new InetSocketAddress(fields[0], Integer.parseInt(fields[1]));
                SAList.add(socketAddress);
                line = reader1.readLine();
            }
        } catch (FileNotFoundException ex) {
            System.out.println("IP list not found");
        } catch (IOException ex) {
            Logger.getLogger(LinkConstructor.class.getName()).log(Level.SEVERE, null, ex);
        }

    }

    public void start() {

        try {
            this.listener1.start();
            this.handshake1.start();

            this.listener1.join();
            this.handshake1.join();

            this.peerBook = new PeerBook(listener1.getGuestList(), handshake1.getPeerList());
            Collections.sort(this.peerBook);

            if (this.peerBook.size() == SAList.size() - 1) {
                for (int i = 0; i < this.peerBook.size(); i++) {
                    Peer p = this.peerBook.get(i);
                    System.out.println(p.ID() + ":" + p.socket().toString());
                }
            } else {
                System.out.print((SAList.size() - this.peerBook.size()) + " not up yet");
            }
        } catch (InterruptedException ex) {
            Logger.getLogger(LinkConstructor.class.getName()).log(Level.SEVERE, null, ex);
        }
    }
    
    public PeerBook getPeerBook(){
        return this.peerBook;
    }
}
