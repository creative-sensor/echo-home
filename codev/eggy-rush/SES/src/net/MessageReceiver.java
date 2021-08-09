/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package net;

import bss.BSSOrderModule;
import core.Egg;
import core.TheMatrix;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.util.Arrays;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.BSSMessage;
import msg.Request;
import msg.Token;
import ses.EggyRush;
import sync.SinghalHeuristic;

/**
 *
 * @author Creativ
 */
public class MessageReceiver extends Thread {

    private BSSOrderModule orderModule;
    private SinghalHeuristic CellExclusion;
    private SinghalHeuristic EggGenSync;
    private TheMatrix matrix;
    private Peer peer;
    

    public void reference(TheMatrix m){
        this.matrix = m;
    }
    public void reference(BSSOrderModule m){
        this.orderModule = m;
    }
    
    public void referenceCellME(SinghalHeuristic ce){
        this.CellExclusion = ce;
    }
    
    public void referenceEG(SinghalHeuristic egs){
        this.EggGenSync = egs;
    }
    public MessageReceiver(Peer p) {
        this.peer = p;
    }

    @Override
    public void run() {

        try {
            BufferedReader bufferedReader = new BufferedReader(
                    new InputStreamReader(peer.socket().getInputStream()));

            while (true) {
                String message = bufferedReader.readLine();

                if (message.startsWith("<bss")) {
                    BSSMessage msg = new BSSMessage(message);
                    orderModule.put(msg);
                }
                if (message.startsWith("<req")) {
                    Request req = new Request(message);
                    //System.out.println("receiver got request cs = " + req.CSID());
                    if(req.CSID() == EggyRush.CELL_SYNC){
                        this.CellExclusion.put(req);
                    }else{
                        this.EggGenSync.put(req);
                    }
                }
                if (message.startsWith("<tok")) {
                    System.out.println(message);
                    Token tok = new Token(message);
                    //System.out.println(tok.SequenceNumber()[0] + ";" + tok.SequenceNumber()[1]+ ";" + tok.SequenceNumber()[2]);
                    //System.out.println(tok.StateVector()[0] + ";" + tok.StateVector()[1]+ ";" + tok.StateVector()[2]);
                    
                    if(tok.getCSID() == EggyRush.CELL_SYNC){
                        this.CellExclusion.put(tok);
                    }else{
                        this.EggGenSync.put(tok);
                    }
                }
                if (message.startsWith("<new")) {
                    Egg newEgg = new Egg(message);
                    matrix.put(newEgg);
                    
                }

                //System.out.println(msg.getContent() + " " + java.util.Arrays.toString(msg.getTimeStamp().value()));



            }
        } catch (IOException ex) {
            Logger.getLogger(MessageReceiver.class.getName()).log(Level.SEVERE, null, ex);
            System.out.print(ex.getMessage());
        }
    }
}
