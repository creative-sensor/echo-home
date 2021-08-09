/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;

import java.util.Queue;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.SESMessage;

/**
 *
 * @author Creativ
 */
public class Motivation extends Thread{

    Queue<SESMessage> waitpool;
    public Motivation(Queue<SESMessage> p) {
        this.waitpool = p;
    }

    
    @Override
    public void run() {
        while(true){
            try {
                Thread.sleep(20000);
            } catch (InterruptedException ex) {
                Logger.getLogger(Motivation.class.getName()).log(Level.SEVERE, null, ex);
            }
            synchronized(waitpool){
                this.waitpool.notify();
            }
        }
    }
    
}
