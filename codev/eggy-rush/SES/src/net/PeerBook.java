/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package net;


import java.net.Socket;
import java.util.LinkedList;
import java.util.List;

/**
 *
 * @author Creativ
 */
public class PeerBook extends LinkedList<Peer> {

    public PeerBook(List<Peer> inbound, List<Peer> outbound) {
        this.addAll(inbound);
        this.addAll(outbound);
    } 
    
    
    public Socket getSocket(int id){
        for(int i=0; i<this.size(); i++){
            Peer p = this.get(i);
            if(p.ID() == id){
                return p.socket();
            }
        }
        return null;
    }
}
