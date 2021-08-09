/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package bss;

import java.util.Iterator;
import java.util.List;
import java.util.concurrent.BlockingQueue;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.BSSMessage;
import ses.BufferRelease;
import ses.TimeStamp;
import ses.VectorClock;

/**
 *
 * @author Creativ
 */
public class Release extends Thread {

    private List<BSSMessage> buffer;
    private BlockingQueue<BSSMessage> PriorWaitPool;
    private VectorClock clock;


    public void reference(List<BSSMessage> buffer){
        this.buffer = buffer;
    }
    
    public void reference(BlockingQueue<BSSMessage> PriorWaitPool){
        this.PriorWaitPool = PriorWaitPool;
    }
    
    public void reference(VectorClock c){
        this.clock = c;
    }
    
    @Override
    public void run() {
        while (true) {
            synchronized (clock) {
                try {
                    clock.wait();
                } catch (InterruptedException ex) {
                    Logger.getLogger(BufferRelease.class.getName()).log(Level.SEVERE, null, ex);
                }
            }

            synchronized (buffer) {
                if (buffer.isEmpty()) {
                    try {
                        buffer.wait();
                    } catch (InterruptedException ex) {
                        Logger.getLogger(BufferRelease.class.getName()).log(Level.SEVERE, null, ex);
                    }
                }

                TimeStamp Now = clock.TimeStamp();
                Iterator<BSSMessage> iter = buffer.iterator();
                while (iter.hasNext()) {
                    BSSMessage msg = iter.next();
                    int source = msg.SenderID();
                    int[] VTm = msg.getTimeStamp().value();

                    boolean condition1 = Now.value()[source] == (VTm[source] - 1);
                    if (condition1) {
                        boolean condition2 = true;
                        for (int i = 0; i < VTm.length; i++) {
                            if (i != source) {
                                if(Now.value()[i] < VTm[i]){
                                    condition2 = false;
                                    break;
                                }
                            }
                        }
                        if(condition2){
                            iter.remove();
                            PriorWaitPool.add(msg);
                        }
                    }
                }
            }
        }
    }
}
