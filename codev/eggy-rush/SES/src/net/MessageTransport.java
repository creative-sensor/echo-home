/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package net;

import bss.BSSOrderModule;
import core.Egg;
import java.io.DataOutputStream;
import java.io.IOException;
import java.net.Socket;
import java.util.Iterator;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.BSSMessage;
import msg.Request;
import msg.Token;
import ses.TimeStamp;

/**
 *
 * @author Creativ
 */
public class MessageTransport {
    private BSSOrderModule orderModule;
    private PeerBook peerBook;
    
    public void reference(BSSOrderModule m){
        this.orderModule = m;
    }

    public void reference(PeerBook pb){
        this.peerBook = pb;
    }
    public TimeStamp broadcast(int id, int action, int para1, int para2) {

        int[] fields = new int[4];
        fields[0] = id;
        fields[1] = action;
        fields[2] = para1;
        fields[3] = para2;

        BSSMessage msg = new BSSMessage();
        msg.setContent(java.util.Arrays.toString(fields));
        orderModule.recordEvent(msg);

        DataOutputStream dataOutputStream = null;
        Iterator<Peer> iter = peerBook.iterator();
        try {
            while (iter.hasNext()) {
                Peer p = iter.next();
                dataOutputStream = new DataOutputStream(p.socket().getOutputStream());
                dataOutputStream.write(msg.toString().getBytes());

            }
        } catch (IOException ex) {
            Logger.getLogger(MessageTransport.class.getName()).log(Level.SEVERE, null, ex);
        }
        return msg.getTimeStamp();

    }

    public void send(Token t, int peerID) {
        DataOutputStream dataOutputStream = null;
        try {
            dataOutputStream  = new DataOutputStream(peerBook.getSocket(peerID).getOutputStream());
            dataOutputStream.writeBytes(t.toString());
        } catch (IOException ex) {
            Logger.getLogger(MessageTransport.class.getName()).log(Level.SEVERE, null, ex);
        }

    }
    
    public void send(Request r, int peerID){
        DataOutputStream dataOutputStream = null;
        try {
            Socket sk = peerBook.getSocket(peerID);
            dataOutputStream  = new DataOutputStream(sk.getOutputStream());
            dataOutputStream.writeBytes(r.toString());
        } catch (IOException ex) {
            Logger.getLogger(MessageTransport.class.getName()).log(Level.SEVERE, null, ex);
        }
    }
    
    public void broadcast(Egg newEgg){
        DataOutputStream dataOutputStream = null;
        Iterator<Peer> iter = peerBook.iterator();
        try {
            while (iter.hasNext()) {
                Peer p = iter.next();
                dataOutputStream = new DataOutputStream(p.socket().getOutputStream());
                dataOutputStream.write(newEgg.toString().getBytes());

            }
        } catch (IOException ex) {
            Logger.getLogger(MessageTransport.class.getName()).log(Level.SEVERE, null, ex);
        }
    }
}
