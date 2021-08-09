/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;

import ai.Cyborg;
import bss.BSSOrderModule;
import core.TheMatrix;
import java.util.Iterator;
import net.LinkConstructor;
import net.MessageReceiver;
import net.MessageTransport;
import net.Peer;
import net.PeerBook;
import sync.SinghalHeuristic;
import visual.ScoreBoard;
import visual.SimpleGraphics;

/**
 *
 * @author Creativ
 */
public class EggyRush {

    public static int myID;
    public static int PlayNum;
    public static boolean auto = false;
    private static int ListenPort = 10000;
    public static final int EGG_SYNC = 0;
    public static final int CELL_SYNC = 1;
    private LinkConstructor linkConstructor;
    private PeerBook peerBook;
    private BSSOrderModule orderModule;
    private SinghalHeuristic CellExclusion;
    private SinghalHeuristic EggGenSync;
    private TheMatrix theMatrix;
    private MessageTransport messageTransport;
    private SimpleGraphics screen;
    private ScoreBoard scoreBoard;
    private Cyborg autobot;

    /**
     * @param args the command line arguments
     *
     */
    public static void main(String[] args) {
        // TODO code application logic here
        EggyRush.myID = Integer.parseInt(args[0]);
        EggyRush.PlayNum = Integer.parseInt(args[1]);
        if (args.length > 2) {
            if (args[2].equalsIgnoreCase("auto")) {
                EggyRush.auto = true;
            }
        }

        EggyRush game = new EggyRush();
        game.start();
    }

    public EggyRush() {
        this.linkConstructor = new LinkConstructor();
        this.orderModule = new BSSOrderModule();
        this.CellExclusion = new SinghalHeuristic(CELL_SYNC);
        this.EggGenSync = new SinghalHeuristic(EGG_SYNC);
        this.theMatrix = new TheMatrix();

        this.messageTransport = new MessageTransport();
        this.scoreBoard = new ScoreBoard();

        if (EggyRush.auto) {
            this.autobot = new Cyborg();
        } else {
            this.screen = new SimpleGraphics();
        }



    }

    private void setupReference() {
        messageTransport.reference(orderModule);
        messageTransport.reference(linkConstructor.getPeerBook());

        CellExclusion.reference(messageTransport);
        EggGenSync.reference(messageTransport);


        theMatrix.reference(messageTransport);
        theMatrix.reference(orderModule.getDeliveryQueue());
        theMatrix.referenceCellME(CellExclusion);
        theMatrix.referenceEggGenME(EggGenSync);

        scoreBoard.reference(theMatrix.getScoreReference());



        if (EggyRush.auto) {
            autobot.reference(theMatrix);
        } else {
            screen.reference(theMatrix);
        }
    }

    public void start() {
        linkConstructor.start();

        setupReference();

        orderModule.start();
        CellExclusion.start();
        EggGenSync.start();
        theMatrix.start();

        scoreBoard.setVisible(true);

        if (EggyRush.auto) {
            autobot.start();
        } else {
            screen.start();
        }

        setReceiverThreads();
    }

    private void setReceiverThreads() {

        Iterator<Peer> iter = linkConstructor.getPeerBook().iterator();
        while (iter.hasNext()) {
            Peer p = iter.next();
            MessageReceiver receiver = new MessageReceiver(p);

            receiver.reference(orderModule);
            receiver.referenceCellME(CellExclusion);
            receiver.referenceEG(EggGenSync);
            receiver.reference(theMatrix);

            receiver.start();
        }
    }
}
