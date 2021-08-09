/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package net;

import java.net.Socket;

/**
 *
 * @author Creativ
 */
public class Peer implements Comparable<Peer> {

    int id;
    Socket sk;

    public Peer(int id, Socket s) {
        this.id = id;
        this.sk = s;
    }
    
    public int ID(){
        return this.id;
                
    }
    
    public Socket socket(){
        return this.sk;
    }

    @Override
    public int compareTo(Peer o) {
        return this.id - o.ID();
    }
    
    
}
