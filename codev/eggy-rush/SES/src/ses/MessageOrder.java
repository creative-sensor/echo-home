/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;


import java.io.BufferedWriter;
import java.io.FileWriter;
import java.io.IOException;
import java.util.List;
import java.util.Queue;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.SESMessage;

/**
 *
 * @author Creativ
 */
public class MessageOrder extends Thread {

   
    BufferedWriter bufferedWriter;
    String VPLog;
    String updatedVPLog;
    Queue<SESMessage> deliveryQueue;
    
    
    public PairVector VP;
    public VectorClock clock;
    public Queue<SESMessage> WaitPool;
    public Queue<SESMessage> PriorWaitPool;
    public List<SESMessage> Buffer;

    
    

    public MessageOrder() {
        try {
            bufferedWriter = new BufferedWriter(new FileWriter("Process-" + EggyRush.myID + "-MessageOrder.log"));
        } catch (IOException ex) {
            Logger.getLogger(MessageOrder.class.getName()).log(Level.SEVERE, null, ex);
        }
    }
    
    private TimeStamp deliver(SESMessage m) {

        // ACTION 1

        synchronized (VP) {
            this.VPLog = VP.toLog();
            PairVector VM = m.getVM();
            for (int i = 0; i < VM.size(); i++) {
                Pair pair = VM.get(i);
                int pid = pair.getID();
                int index = VP.hasEntry(pid);
                if (index == -1) {
                    if (pid != EggyRush.myID) {
                        VP.add(pair);
                    }
                } else {
                    if (pid != EggyRush.myID) {
                        VP.get(index).update(pair);
                    }
                }
            }
            this.updatedVPLog = VP.toLog();
            synchronized(deliveryQueue){
                deliveryQueue.add(m);
                deliveryQueue.notify();
            }
        }


        // ACTION 2
        return clock.update(m.getTimeStamp());

    }

    private void writeLog(String status, SESMessage msg, TimeStamp Now, TimeStamp Updated, String NowVP, String updatedVP) {
        try {
            String log = status + ": " + msg.toLog();
            log += "\r\n#CLOCK --------------\r\n" + "\tNow:\r\n" + Now.toLog() ;
            if (Updated != null) {
                log += "\r\n\tUpdated:\r\n" + Updated.toLog();
            }
            log += "\r\n#VP -----------------\r\n" + "\tNow:\r\n" + NowVP;
            if (updatedVP != null) {
                log += "\r\n\tUpdated:\r\n" + updatedVP;
            }
            this.bufferedWriter.write(log);
            this.bufferedWriter.newLine();
            this.bufferedWriter.write("----------------------------------------------------");
            this.bufferedWriter.newLine();
            this.bufferedWriter.flush();
        } catch (IOException ex) {
            Logger.getLogger(MessageOrder.class.getName()).log(Level.SEVERE, null, ex);
        }

    }

    @Override
    public void run() {

        int msgCount = 0;
        while (msgCount < 16) {
            SESMessage msg;
            TimeStamp Now = clock.TimeStamp();
            if (!PriorWaitPool.isEmpty()) {
                msg = PriorWaitPool.remove();

                TimeStamp updated = deliver(msg);
                writeLog("DELIVERED (buffered)", msg, Now, updated, VPLog, updatedVPLog);

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

                    PairVector vm = msg.getVM();
                    int index = vm.hasEntry(EggyRush.myID);

                    if (index == -1) { // vm has no entry of this site's pid
                        TimeStamp updated = deliver(msg);
                        writeLog("DELIVERED immediate", msg, Now, updated, VPLog, updatedVPLog);

                    } else { // vm does have an entry of this site's pid

                        TimeStamp tPj = vm.get(index).getTimeStamp();
                        if (Now.isNotLessThan(tPj)) {
                            TimeStamp updated = deliver(msg);
                            writeLog("DELIVERED", msg, Now, updated, VPLog, this.updatedVPLog);
                        } else {

                            synchronized (Buffer) {
                                Buffer.add(msg);
                                Buffer.notify();
                                writeLog("BUFFERED", msg, Now, null, VPLog, null);
                            }
                        }
                    }

                }
            }
        }
    }
}