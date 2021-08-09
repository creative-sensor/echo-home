/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;

import java.io.BufferedWriter;
import java.io.FileWriter;
import java.io.IOException;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.BlockingQueue;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.SESMessage;

/**
 *
 * @author Creativ
 */
public class BufferRelease extends Thread {
    public VectorClock clock;
    public List<SESMessage> Buffer;
    public BlockingQueue<SESMessage> PriorWaitPool;
    public BlockingQueue<SESMessage> WaitPool;
    
    BufferedWriter bufferedWriter;

    public BufferRelease() {
        try {
            this.bufferedWriter = new BufferedWriter(new FileWriter("Process-" + EggyRush.myID + "-BufferRelease.log"));
        } catch (IOException ex) {
            Logger.getLogger(BufferRelease.class.getName()).log(Level.SEVERE, null, ex);
        }
    }

    void writeLog(TimeStamp Now, SESMessage msg) {
        try {
            this.bufferedWriter.write("Clock: " + Now.toLog() + "release for " + msg.toLog());
            this.bufferedWriter.write("-----------------------------------------------------");
            this.bufferedWriter.newLine();
            this.bufferedWriter.flush();
        } catch (IOException ex) {
            Logger.getLogger(BufferRelease.class.getName()).log(Level.SEVERE, null, ex);
        }
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

            synchronized (Buffer) {
                if (Buffer.isEmpty()) {
                    try {
                        Buffer.wait();
                    } catch (InterruptedException ex) {
                        Logger.getLogger(BufferRelease.class.getName()).log(Level.SEVERE, null, ex);
                    }
                }

                TimeStamp Now = clock.TimeStamp();
                Iterator<SESMessage> iter = Buffer.iterator();
                while (iter.hasNext()) {
                    SESMessage msg = iter.next();
                    int index = msg.getVM().hasEntry(EggyRush.myID);
                    TimeStamp tPj = msg.getVM().get(index).getTimeStamp();
                    if (Now.isNotLessThan(tPj)) { // get crazy with logic reasoning! haizz
                        PriorWaitPool.add(msg);
                        iter.remove();
                        writeLog(Now, msg);
                    }
                }
            }
            synchronized (WaitPool) {
                WaitPool.notify();
            }
        }
    }
}
