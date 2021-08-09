/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package bss;

import java.util.List;
import java.util.Queue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.BSSMessage;
import ses.MessageOrder;
import ses.TimeStamp;
import ses.VectorClock;

/**
 *
 * @author Creativ
 */
public class Order extends Thread {

    private VectorClock clock;
    private Queue<BSSMessage> WaitPool;
    private Queue<BSSMessage> PriorWaitPool;
    private List<BSSMessage> Buffer;
    private Queue<BSSMessage> deliveryQueue;

    public Order() {
        this.deliveryQueue = new LinkedBlockingQueue<>();
    }

    public Queue<BSSMessage> getDeliveryQueue() {
        return this.deliveryQueue;
    }

    private TimeStamp deliver(BSSMessage m) {
        synchronized (deliveryQueue) {
            deliveryQueue.add(m);
            deliveryQueue.notify();
        }
        return clock.update(m.getTimeStamp());
    }

    public void reference(VectorClock clock) {
        this.clock = clock;
    }

    public void reference(Queue<BSSMessage> WaitPool) {
        this.WaitPool = WaitPool;
    }

    public void referencePrior(Queue<BSSMessage> PriorWaitPool) {
        this.PriorWaitPool = PriorWaitPool;
    }

    public void reference(List<BSSMessage> buffer) {
        this.Buffer = buffer;
    }

    @Override
    public void run() {
        int msgCount = 0;
        while (msgCount < 16) {
            BSSMessage msg;
            if (!PriorWaitPool.isEmpty()) {
                msg = PriorWaitPool.remove();
                TimeStamp updated = deliver(msg);
                //System.out.println("new message delivered: " + msg.toString());

            } else {
                synchronized (WaitPool) {
                    if (WaitPool.isEmpty()) {
                        try {
                            WaitPool.wait();
                        } catch (InterruptedException ex) {
                            Logger.getLogger(MessageOrder.class.getName()).log(Level.SEVERE, null, ex);
                        }
                    }
                    if (WaitPool.isEmpty()) {
                        continue;
                    }

                    msg = WaitPool.remove();

                    TimeStamp Now = clock.TimeStamp();
                    int source = msg.SenderID();
                    int[] VTm = msg.getTimeStamp().value();
                    //System.out.println("source = " +source);
                    //System.out.println("Clock now: " +Now.toString());
                    //System.out.println("Message now: " +msg.getTimeStamp().toString());
                    boolean condition1 = (Now.value()[source] == (VTm[source] - 1));
                    boolean condition2 = true;
                    for (int i = 0; i < VTm.length; i++) {
                        if (i != source) {
                            if (Now.value()[i] < VTm[i]) {
                                condition2 = false;
                                //System.out.println("condition2: VTP[" + i + "] +" + Now.value()[i] + " <  VTm[i] " + VTm[i]);
                                break;
                            }
                        }
                    }
                    if (condition1 && condition2) {
                        TimeStamp updated = deliver(msg);
                        //System.out.println("new message delivered: " + msg.toString());
                    } else {
                        synchronized (this.Buffer) {
                            Buffer.add(msg);
                            Buffer.notify();
                        }
                        //System.out.println("new message buffered: " + msg.toString());

                    }
                }
            }
        }
    }
}
